#!/usr/bin/env python3
"""
Generic queue worker — the thing you run on each self-hosted runner.

Instead of the website dispatching one specific workflow per request, work now
lives in the `jobs` table (see db/migration_job_queue.sql). This worker loops:

    claim the next pending job  (claim_job: FOR UPDATE SKIP LOCKED)
      -> run it (re-using sync.py / reporter_sync.py / roster_sync.py /
         live_watch.py exactly as the old per-kind workflows did)
      -> finish_job(success|error)
      -> repeat

Because claim_job uses SKIP LOCKED, running this on N runners gives you N workers
that pick up DIFFERENT jobs and run them in parallel — e.g. the live-watch daemon
on one worker while a backfill runs on another. A background thread heartbeats the
job's lease so a crashed worker's job is reclaimed (reclaim_jobs) and retried.

It drains the queue while alive, then exits once it has been idle for a while
(idle_exit cycles) or hits max-minutes — same "exit and let the workflow end"
pattern as the old live watcher, so the runner is freed and a fresh dispatch /
cron wake starts a new worker.

Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (via env or .env, like sync.py).

    python worker.py                  # take any kind of job
    python worker.py --kinds watch    # only ever run the live-watch daemon
    python worker.py --kinds round,match,backfill,reporter   # never watch
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

# Give this process a stable worker id BEFORE importing modules whose status
# files we want namespaced per worker (jobstatus reads WORKER_ID at import).
if not os.environ.get("WORKER_ID"):
    os.environ["WORKER_ID"] = (
        os.environ.get("RUNNER_NAME")
        or f"{socket.gethostname()}-{os.getpid()}")
WORKER_ID = os.environ["WORKER_ID"]

import sync  # noqa: E402  (after WORKER_ID is set)

# kind -> the module whose main() runs it (re-using the existing entry points).
KIND_MODULE = {
    "round": "sync", "match": "sync", "backfill": "sync",
    "scheduled": "sync", "reporter_sweep": "sync",
    "reporter": "reporter_sync",
    "watch": "live_watch",
    "teams": "roster_sync", "roster": "roster_sync", "players": "roster_sync",
    "comp_full": "roster_sync", "comp_players": "roster_sync",
}

# Human label shown in the tray (JOB_KIND), per kind.
KIND_LABEL = {
    "round": "League matches", "match": "Match", "backfill": "Backfill",
    "scheduled": "Scheduled sweep", "reporter_sweep": "Reporter sweep",
    "reporter": "Reporter", "watch": "Live watch",
    "teams": "Squads", "roster": "Roster", "players": "Player",
    "comp_full": "Full squads", "comp_players": "Squad positions",
}

# Generic param -> env var. The job's params mirror the old workflow inputs.
PARAM_ENV = {
    "match": "IN_MATCH", "jornada": "IN_JORNADA", "competition": "IN_COMPETITION",
    "force": "IN_FORCE", "match_id": "IN_MATCH_ID", "abola_url": "IN_ABOLA_URL",
    "team": "IN_TEAM", "player": "IN_PLAYER",
    # The website pre-creates a crawl_runs row and passes its id so the existing
    # entry point UPDATES that row (keeping the dashboard's status polling working)
    # instead of creating a fresh one.
    "run_id": "IN_RUN_ID",
}

# Env vars the worker may set per job; cleared before each so a previous job's
# inputs never leak into the next one this same worker runs.
_JOB_ENV = set(PARAM_ENV.values()) | {
    "IN_BACKFILL", "IN_FORCE", "IN_SCHEDULED", "IN_REPORTER_SWEEP",
    "IN_FULL", "IN_PLAYERS_ONLY", "IN_RUN_ID", "WATCH_DAEMON", "JOB_KIND",
}


def _target_fallback(kind: str, params: dict) -> None:
    """Let a job specify a single generic `target` instead of the explicit key,
    so a hand-enqueued job (or a simple website payload) still works."""
    t = params.get("target")
    if not t:
        return
    if kind == "round":
        params.setdefault("jornada", t)
    elif kind in ("match", "watch"):
        params.setdefault("match", t)
    elif kind == "backfill" or kind in ("teams", "roster", "comp_full",
                                        "comp_players"):
        params.setdefault("competition", t)
    elif kind == "reporter":
        params.setdefault("match_id", t)
    elif kind == "players":
        params.setdefault("player", t)


def _prepare_env(kind: str, params: dict) -> None:
    """Translate a job into the IN_* env the existing entry points read."""
    for k in _JOB_ENV:          # clear the previous job's inputs
        os.environ.pop(k, None)

    _target_fallback(kind, params)
    for key, env in PARAM_ENV.items():
        v = params.get(key)
        if v not in (None, ""):
            os.environ[env] = str(v)

    # Kind-derived flags (what the website used to bake into the workflow inputs).
    if kind == "backfill":
        os.environ["IN_BACKFILL"] = "true"
    elif kind == "scheduled":
        os.environ["IN_SCHEDULED"] = "true"
    elif kind == "reporter_sweep":
        os.environ["IN_REPORTER_SWEEP"] = "true"
    elif kind == "comp_full":
        os.environ["IN_FULL"] = "true"
    elif kind == "comp_players":
        os.environ["IN_PLAYERS_ONLY"] = "true"
    elif kind == "watch":
        os.environ["WATCH_DAEMON"] = "1"

    os.environ["JOB_KIND"] = KIND_LABEL.get(kind, kind)


def run_job(job: dict) -> None:
    """Execute one claimed job by re-using the matching module's main()."""
    kind = job["kind"]
    params = dict(job.get("params") or {})
    module_name = KIND_MODULE.get(kind)
    if not module_name:
        raise ValueError(f"unknown job kind: {kind!r}")

    _prepare_env(kind, params)

    import importlib
    module = importlib.import_module(module_name)

    # The entry points use argparse; feed them only the program name so they fall
    # back to the IN_* env we just set (the exact path the workflows used).
    old_argv = sys.argv
    sys.argv = [f"{module_name}.py"]
    try:
        rc = module.main()
    finally:
        sys.argv = old_argv
    if rc not in (0, None):
        raise RuntimeError(f"{module_name}.main() exited with {rc}")


class _Heartbeat:
    """Background lease-extender for the job currently running."""

    def __init__(self, sb: sync.Supabase, job_id: int, *,
                 lease_seconds: int, interval: float):
        self._sb = sb
        self._job_id = job_id
        self._lease = lease_seconds
        self._interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not self._sb.heartbeat_job(self._job_id, WORKER_ID,
                                              lease_seconds=self._lease):
                    print(f"  ! lost lease on job {self._job_id} "
                          "(reclaimed?) — another worker may take it",
                          file=sys.stderr)
            except Exception as e:  # noqa: BLE001 - never let a beat kill the job
                print(f"  ! heartbeat error on job {self._job_id}: {e}",
                      file=sys.stderr)

    def __enter__(self) -> "_Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


def work_loop(sb: sync.Supabase, *, kinds: list[str] | None,
              lease_seconds: int, heartbeat_seconds: float, poll_interval: float,
              idle_exit_cycles: int, max_minutes: float) -> None:
    print(f"Worker {WORKER_ID} up (kinds={kinds or 'any'}).", file=sys.stderr)
    started = time.time()
    idle = 0

    while True:
        if (time.time() - started) / 60 > max_minutes:
            print("Max worker time reached — exiting.", file=sys.stderr)
            break

        try:
            job = sb.claim_job(WORKER_ID, lease_seconds=lease_seconds,
                               kinds=kinds)
        except Exception as e:  # noqa: BLE001 - transient DB error: back off, retry
            print(f"  ! claim error: {e}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        if not job:
            idle += 1
            if idle >= idle_exit_cycles:
                print("Queue empty — worker idle, exiting.", file=sys.stderr)
                break
            time.sleep(poll_interval)
            continue
        idle = 0

        jid, kind = job["id"], job["kind"]
        print(f"[{time.strftime('%H:%M:%S')}] claimed job {jid} ({kind})",
              file=sys.stderr)
        try:
            with _Heartbeat(sb, jid, lease_seconds=lease_seconds,
                            interval=heartbeat_seconds):
                run_job(job)
            sb.finish_job(jid, "done")
            print(f"  job {jid} done.", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - one bad job must not kill the worker
            err = str(e)[:2000]
            print(f"  ! job {jid} failed: {err}", file=sys.stderr)
            try:
                sb.finish_job(jid, "error", err)
            except Exception as e2:  # noqa: BLE001
                print(f"  !! could not record failure for job {jid}: {e2}",
                      file=sys.stderr)


def main() -> int:
    sync._load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kinds",
                    help="Comma-separated job kinds this worker will take "
                         "(default: any). e.g. 'watch' or 'round,match,reporter'.")
    ap.add_argument("--lease-seconds", type=int, default=300,
                    help="Lease length; reclaimed if not heartbeat within this.")
    ap.add_argument("--heartbeat-seconds", type=float, default=60,
                    help="How often to extend the lease while a job runs.")
    ap.add_argument("--poll-interval", type=float, default=10,
                    help="Seconds between claim attempts when idle.")
    ap.add_argument("--idle-exit-cycles", type=int, default=6,
                    help="Exit after this many empty polls (frees the runner).")
    ap.add_argument("--max-minutes", type=float, default=300,
                    help="Hard cap on how long this worker process runs.")
    args = ap.parse_args()

    kinds = ([k.strip() for k in args.kinds.split(",") if k.strip()]
             if args.kinds else None)

    sb = sync.Supabase()
    work_loop(sb, kinds=kinds, lease_seconds=args.lease_seconds,
              heartbeat_seconds=args.heartbeat_seconds,
              poll_interval=args.poll_interval,
              idle_exit_cycles=args.idle_exit_cycles,
              max_minutes=args.max_minutes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
