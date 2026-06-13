#!/usr/bin/env python3
"""
Best-effort progress heartbeat for the runner tray (tools/runner_tray.py).

The crawl / roster / reporter jobs run on the self-hosted runner. This writes a
small JSON file the tray reads, so at a glance you can see:
  - what the job is doing right now (stage text),
  - roughly how far along it is (current / total),
  - that it's still moving (the tray shows how long since the last update),
  - and the outcome of the last run (so a FAILURE's reason is visible in the
    tray without opening the website).

It mirrors live_watch's `_watch_status.json` convention — same actions-runner
folder, same `updated` epoch field the tray already understands.

Everything here is wrapped so it can NEVER raise into a sync: a missing folder,
a locked file, a serialization error — all silently no-op. Importing it has no
side effects and pulls in stdlib only (safe for crawler.py to import).
"""

from __future__ import annotations

import json
import os
import time

# The exact path the tray watches (same folder the runner + live_watch use).
# When several workers run on one PC (one per self-hosted runner), each must
# write its OWN status file or they clobber each other — so we suffix the file
# with WORKER_ID when set. A single-runner setup (no WORKER_ID) keeps the legacy
# unsuffixed name, and the tray globs _job_status*.json to find them all.
_WORKER = "".join(c if (c.isalnum() or c in "-_") else "_"
                  for c in os.environ.get("WORKER_ID", ""))
_FILENAME = f"_job_status_{_WORKER}.json" if _WORKER else "_job_status.json"
STATUS_FILE = os.path.join(
    os.environ.get("USERPROFILE", "."), "actions-runner", _FILENAME)

# Human label for the kind of job, set by the workflow step (JOB_KIND); the tray
# shows it so you know which job is running. Falls back to a generic word.
JOB = os.environ.get("JOB_KIND") or "Crawl"

_started = time.time()


def _write(payload: dict) -> None:
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except Exception:  # noqa: BLE001 - status is best-effort, never fatal
        pass


# Which worker (runner) produced this status, so the tray can label it per runner.
WORKER = os.environ.get("WORKER_ID") or ""


def report(stage: str, *, current: int | None = None,
           total: int | None = None) -> None:
    """Heartbeat: what the job is doing now, with optional i/total progress."""
    _write({"updated": time.time(), "job": JOB, "worker": WORKER,
            "phase": "running", "stage": stage, "current": current,
            "total": total, "started": _started})


def done(result: str, *, message: str | None = None) -> None:
    """Final outcome. `result` is 'success' or 'error'; `message` is a short
    summary or the error text (the tray surfaces it so a failure is explained)."""
    _write({"updated": time.time(), "job": JOB, "worker": WORKER,
            "phase": "done", "result": result, "message": (message or "")[:600],
            "finished": time.time(), "started": _started})
