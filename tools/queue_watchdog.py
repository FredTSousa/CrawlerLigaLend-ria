#!/usr/bin/env python3
"""
Queue watchdog — an independent backstop so the job queue can't silently stall.

Normally Supabase wakes workers: pg_cron's per-minute 'queue-tick' calls
dispatch_worker() and reclaim_jobs(). If any piece of that breaks — a bug in
the SQL (e.g. a long live-watch counted as "a worker is active"), an expired
gh_dispatch_token in Vault, pg_cron / pg_net down — jobs just sit 'pending'
with nothing to tell you.

This runs from GitHub's own scheduler on a GitHub-hosted runner (so it doesn't
occupy the self-hosted runners) and uses only plain REST reads/writes on the
jobs table plus the GitHub API — no database functions. Each run:

  1. Returns jobs whose lease expired (worker died) to 'pending' — the same
     thing reclaim_jobs() does.
  2. If a job has been pending longer than STALE_MIN, nothing is about to claim
     it (no queued worker run, no running non-watch job), wakes a worker by
     dispatching worker.yml — and exits non-zero, so the run shows red in
     Actions: the queue healed itself, but the normal wake-up path is broken
     and worth a look.

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, GITHUB_TOKEN, GITHUB_REPOSITORY.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# The normal path wakes a worker within ~1 minute; a job pending this long means
# that path didn't fire.
STALE_MIN = 5
# A live-watch daemon holds its worker for hours and never claims other jobs,
# so a running watch doesn't mean anything will pick up the queue.
NON_DRAINING_KINDS = {"watch"}

SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]
GH_REPO = os.environ["GITHUB_REPOSITORY"]

sb = requests.Session()
sb.headers.update({"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
                   "Content-Type": "application/json"})
gh = requests.Session()
gh.headers.update({"Authorization": f"Bearer {GH_TOKEN}",
                   "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"})


def rest(method: str, path: str, **kw) -> list:
    r = sb.request(method, f"{SB_URL}/rest/v1/{path}", timeout=30, **kw)
    r.raise_for_status()
    return r.json() if r.content else []


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def reclaim_expired(now: datetime) -> int:
    dead = rest("GET", "jobs?status=eq.running"
                f"&lease_expires_at=lt.{iso(now)}"
                "&select=id,kind,attempts,max_attempts,last_error")
    for j in dead:
        give_up = j["attempts"] >= j["max_attempts"]
        rest("PATCH", f"jobs?id=eq.{j['id']}&status=eq.running", json={
            "status": "error" if give_up else "pending",
            "last_error": j.get("last_error") or "lease expired (worker died)",
            "finished_at": iso(now) if give_up else None,
            "claimed_by": None, "lease_expires_at": None,
        })
        print(f"reclaimed job {j['id']} ({j['kind']}) -> "
              f"{'error' if give_up else 'pending'}")
    return len(dead)


def recent_queued_worker_runs(now: datetime) -> int:
    """worker.yml runs waiting for a runner, queued within the last 30 min.
    GitHub can leave orphaned runs 'queued' for weeks without ever assigning
    them; counting those would make the watchdog think help is always coming."""
    r = gh.get(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/"
               f"worker.yml/runs", params={"status": "queued", "per_page": 50},
               timeout=30)
    r.raise_for_status()
    since = iso(now - timedelta(minutes=30))
    return sum(1 for run in r.json().get("workflow_runs", [])
               if run["created_at"] >= since)


def main() -> int:
    now = datetime.now(timezone.utc)
    reclaimed = reclaim_expired(now)

    stale = rest("GET", "jobs?status=eq.pending"
                 f"&created_at=lt.{iso(now - timedelta(minutes=STALE_MIN))}"
                 "&select=id,kind,created_at&order=created_at")
    if not stale:
        print(f"OK — no job pending > {STALE_MIN} min "
              f"({reclaimed} reclaimed).")
        return 0

    summary = ", ".join(f"{j['kind']}#{j['id']} since {j['created_at'][:16]}"
                        for j in stale[:10])

    running = rest("GET", "jobs?status=eq.running&select=kind")
    draining = [j["kind"] for j in running
                if j["kind"] not in NON_DRAINING_KINDS]
    if draining:
        print(f"Pending ({summary}) but a worker is busy with {draining}; "
              "it will claim them next.")
        return 0
    if recent_queued_worker_runs(now):
        print(f"Pending ({summary}) and a worker run is already queued on "
              "GitHub — waiting for a free self-hosted runner.")
        return 0

    r = gh.post(f"https://api.github.com/repos/{GH_REPO}/actions/workflows/"
                "worker.yml/dispatches", json={"ref": "main"}, timeout=30)
    r.raise_for_status()
    print(f"::error::Queue was stalled — {len(stale)} job(s) pending > "
          f"{STALE_MIN} min with no worker coming ({summary}). Dispatched "
          "worker.yml. The normal Supabase wake-up (pg_cron queue-tick -> "
          "dispatch_worker) isn't firing; check it and the gh_dispatch_token "
          "in Vault.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
