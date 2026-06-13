#!/usr/bin/env pythonw
"""
Crawler LLP — system-tray controller for the self-hosted GitHub Actions runner.

Shows a tray icon that is GREEN while the runner is up and GREY when it's
stopped, and lets you start / stop / quit it from the tray menu. Launch it with
pythonw (no console window):

    pythonw tools/runner_tray.py

It manages the runner installed at  %USERPROFILE%\\actions-runner  (run.cmd).
"""

from __future__ import annotations

import ctypes
import glob
import json
import os
import re
import subprocess
import threading
import time

import pystray
from PIL import Image, ImageDraw

try:
    import psutil  # per-runner liveness; optional (falls back to tray-launched only)
except ImportError:  # pragma: no cover
    psutil = None

APP_NAME = "Crawler LLP"
RUNNER_DIR = os.path.join(os.environ.get("USERPROFILE", ""), "actions-runner")
WATCH_STATUS = os.path.join(RUNNER_DIR, "_watch_status.json")
# Each worker writes its own _job_status_<worker>.json (a single-runner setup
# keeps the legacy _job_status.json); the glob matches both.
JOB_STATUS_GLOB = os.path.join(RUNNER_DIR, "_job_status*.json")
LISTENER = "Runner.Listener.exe"
WORKER = "Runner.Worker.exe"  # exists only while a job (e.g. a live watch) runs

# Windows: don't pop up console windows for our helper processes.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

GREEN = (40, 180, 80, 255)
BLUE = (60, 140, 240, 255)
GREY = (120, 120, 120, 255)
RED = (210, 60, 60, 255)

# Processes we launched, keyed by runner folder. The tray manages the primary
# actions-runner folder plus any actions-runner-* siblings (e.g. the second
# runner "LLPCrawler Second runner" in actions-runner-2), so several workers run
# in parallel. Their job-status files all land in the primary folder (jobstatus.py
# writes to %USERPROFILE%\actions-runner), which the tray already globs.
_procs: dict[str, subprocess.Popen] = {}


# ----------------------------------------------------------------------------
# Runner state + control
# ----------------------------------------------------------------------------


def _runner_dirs() -> list[str]:
    """Every runner folder to manage: the primary one, then actions-runner-*
    siblings, each identified by containing a run.cmd."""
    base = os.environ.get("USERPROFILE", "")
    dirs: list[str] = []
    if os.path.exists(os.path.join(RUNNER_DIR, "run.cmd")):
        dirs.append(RUNNER_DIR)
    for path in sorted(glob.glob(os.path.join(base, "actions-runner-*"))):
        if os.path.isdir(path) and os.path.exists(os.path.join(path, "run.cmd")):
            dirs.append(path)
    return dirs


def _proc_running(image: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, creationflags=_NO_WINDOW).stdout
        return image.lower() in out.lower()
    except Exception:
        return False


def _runner_name(d: str) -> str:
    """The GitHub runner's display name (from its .runner config), so the tray
    shows 'LLPCrawler Second runner' rather than the folder name."""
    try:
        # .runner is written with a UTF-8 BOM, so decode with utf-8-sig.
        with open(os.path.join(d, ".runner"), encoding="utf-8-sig") as fh:
            name = json.load(fh).get("agentName")
        if name:
            return name
    except Exception:
        pass
    return os.path.basename(d)


def _listener_dirs() -> set[str]:
    """Runner folders that currently have a live Runner.Listener.exe — detected
    by the process's exe path, so it works no matter who started the runner."""
    if psutil is None:
        return set()
    dirs = {os.path.normpath(d).lower(): d for d in _runner_dirs()}
    live: set[str] = set()
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            if (proc.info["name"] or "").lower() != LISTENER.lower():
                continue
            exe = os.path.normpath(proc.info["exe"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
            continue
        for key, orig in dirs.items():
            if exe.startswith(key + os.sep):  # <dir>\bin\Runner.Listener.exe
                live.add(orig)
    return live


def _runner_dir_up(d: str) -> bool:
    """True if this specific runner folder is running (live listener, or a tray-
    launched process for it)."""
    if d in _listener_dirs():
        return True
    p = _procs.get(d)
    return p is not None and p.poll() is None


def _runner_up() -> bool:
    """True if any runner is running."""
    if any(_runner_dir_up(d) for d in _runner_dirs()):
        return True
    # Fallback when psutil is unavailable and nothing is tray-launched.
    return psutil is None and _proc_running(LISTENER)


def _job_running() -> bool:
    """True while the runner is executing a job (e.g. a live watch)."""
    return _proc_running(WORKER)


def _watch_matches() -> list:
    """Matches currently being watched, from the watcher's status file."""
    try:
        with open(WATCH_STATUS, encoding="utf-8") as fh:
            data = json.load(fh)
        if time.time() - float(data.get("updated", 0)) > 120:
            return []  # stale -> watcher not running
        return data.get("matches", [])
    except Exception:
        return []


def _match_label(m: dict) -> str:
    mm = re.search(r"/jogo/([^/]+)/", m.get("url", ""))
    slug = mm.group(1) if mm else str(m.get("id", "?"))
    return f"{slug} {m.get('minute') or ''}".strip()


def _job_statuses() -> list[dict]:
    """All workers' job heartbeats (one file per worker). Each dict gets a
    '_path' so callers can act on the specific file (e.g. dismiss an error)."""
    out: list[dict] = []
    for path in glob.glob(JOB_STATUS_GLOB):
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            d["_path"] = path
            out.append(d)
        except Exception:
            pass
    return out


def _job_status() -> dict | None:
    """The single most relevant job heartbeat: prefer a currently-running one,
    and among ties the most recently updated."""
    items = _job_statuses()
    if not items:
        return None
    running = [d for d in items if d.get("phase") == "running"]
    pool = running or items
    return max(pool, key=lambda d: float(d.get("updated", 0)))


def _fmt_age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    return f"{s // 3600}h ago"


def _progress(js: dict) -> str:
    cur, total = js.get("current"), js.get("total")
    return f" [{cur}/{total}]" if cur and total else ""


def _last_error() -> str | None:
    """Full text of a failed job's error, across all workers (most recent)."""
    failed = [d for d in _job_statuses()
              if d.get("phase") == "done" and d.get("result") == "error"]
    if not failed:
        return None
    js = max(failed, key=lambda d: float(d.get("updated", 0)))
    return js.get("message") or "(no detail recorded)"


def show_last_error(icon=None, item=None) -> None:
    msg = _last_error()
    if not msg:
        return

    # Show the box on its OWN thread. pystray runs menu callbacks on the tray's
    # UI thread, and a modal MessageBox there freezes the whole tray (stuck icon,
    # no status updates) until it's dismissed. A separate thread keeps the tray
    # live and lets the dialog be closed independently.
    def _box() -> None:
        try:  # MB_OK | MB_ICONERROR | MB_TOPMOST | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(
                0, msg, f"{APP_NAME} — last job error", 0x10 | 0x40000 | 0x10000)
        except Exception:
            pass

    threading.Thread(target=_box, daemon=True).start()


def dismiss_error(icon=None, item=None) -> None:
    """Clear the last-error indicator (red icon + status) by removing the failed
    workers' status files. The next job writes a fresh status anyway."""
    for d in _job_statuses():
        if d.get("phase") == "done" and d.get("result") == "error":
            try:
                os.remove(d["_path"])
            except Exception:
                pass
    if icon is not None:
        icon.update_menu()


def stop_job(icon=None, item=None) -> None:
    """Cancel the current job (kills the worker) but keep the runner online."""
    subprocess.run(["taskkill", "/F", "/T", "/IM", WORKER],
                   creationflags=_NO_WINDOW, capture_output=True)


def start_runner(icon=None, item=None) -> None:
    """Start every runner folder we aren't already running (one process each)."""
    for d in _runner_dirs():
        if _runner_dir_up(d):
            continue  # already running (by us or externally) — don't double-start
        run_cmd = os.path.join(d, "run.cmd")
        if not os.path.exists(run_cmd):
            continue
        log = open(os.path.join(d, "_tray_run.log"), "a",
                   encoding="utf-8", errors="replace")
        _procs[d] = subprocess.Popen(
            ["cmd", "/c", run_cmd], cwd=d,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=_NO_WINDOW)


def stop_runner(icon=None, item=None) -> None:
    """Stop all runners we launched, plus any listener started outside the app."""
    for p in list(_procs.values()):
        if p.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           creationflags=_NO_WINDOW, capture_output=True)
    _procs.clear()
    subprocess.run(["taskkill", "/F", "/T", "/IM", LISTENER],
                   creationflags=_NO_WINDOW, capture_output=True)


def open_folder(icon=None, item=None) -> None:
    if os.path.isdir(RUNNER_DIR):
        os.startfile(RUNNER_DIR)  # noqa: S606 - user-initiated


# ----------------------------------------------------------------------------
# Tray icon
# ----------------------------------------------------------------------------


def _make_icon(up: bool, busy: bool = False, error: bool = False) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not up:
        fill = GREY
    elif busy:
        fill = BLUE
    elif error:
        fill = RED      # idle, but the last job failed — draws attention
    else:
        fill = GREEN
    d.ellipse((6, 6, 58, 58), fill=fill,
              outline=(255, 255, 255, 255), width=3)
    # A small "C" so it reads as Crawler LLP at a glance.
    d.text((22, 18), "C", fill=(255, 255, 255, 255))
    return img


def _state_label() -> str:
    if not _runner_up():
        return "Stopped"
    watching = _watch_matches()
    if watching:
        return "Watching " + ", ".join(_match_label(m) for m in watching)

    js = _job_status()
    age = (time.time() - float(js.get("updated", 0))) if js else None

    if _job_running():
        # A job is executing. If the heartbeat is live, show exactly what stage
        # it's on + progress + how long since the last update (so you can see it
        # moving, and spot a stall). Otherwise fall back to the generic label.
        if js and js.get("phase") == "running":
            return (f"{js.get('job', 'Job')}: {js.get('stage', '…')}"
                    f"{_progress(js)} · {_fmt_age(age)}")
        return "Working (job running)"

    # No job running: report the outcome of the last one so a failure is visible.
    if js and js.get("phase") == "done":
        res = js.get("result", "done")
        msg = js.get("message") or ""
        when = _fmt_age(age) if age is not None else ""
        tail = f" — {msg}" if msg else ""
        return f"Last {js.get('job', 'job')}: {res}{tail} ({when})"
    if js and js.get("phase") == "running":
        # Heartbeat left mid-run but no worker alive -> the job was interrupted.
        return (f"Last {js.get('job', 'job')}: interrupted at "
                f"{js.get('stage', '?')} ({_fmt_age(age)})")
    return "Online (idle)"


def _active() -> bool:
    """A job or a live watch is in progress (icon goes blue)."""
    return _job_running() or bool(_watch_matches())


def _status_text(_=None) -> str:
    return f"Status: {_state_label()}"


def _runners_text(_=None) -> str:
    """One line naming every managed runner with a ● (up) / ○ (down) marker."""
    dirs = _runner_dirs()
    if not dirs:
        return "Runners: none found"
    parts = [f"{_runner_name(d)} {'●' if _runner_dir_up(d) else '○'}" for d in dirs]
    return "Runners: " + "  ·  ".join(parts)


def _on_exit(icon, item) -> None:
    stop_runner(icon)
    icon.stop()


def _monitor(icon: pystray.Icon) -> None:
    last = None
    while True:
        label = _state_label()
        failed = not _active() and _last_error() is not None
        state = (_runner_up(), _active(), failed, label)
        if state != last:
            up, active, err, _ = state
            icon.icon = _make_icon(up, active, err)
            icon.title = f"{APP_NAME} — {label}"
            icon.update_menu()
            last = state
        time.sleep(3)


def _setup(icon: pystray.Icon) -> None:
    icon.visible = True
    start_runner(icon)               # auto-start the runner on launch
    threading.Thread(target=_monitor, args=(icon,), daemon=True).start()


def main() -> None:
    menu = pystray.Menu(
        pystray.MenuItem(_status_text, None, enabled=False),
        pystray.MenuItem(_runners_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start runner", start_runner,
                         visible=lambda i: any(not _runner_dir_up(d)
                                               for d in _runner_dirs())),
        pystray.MenuItem("Show last error…", show_last_error,
                         visible=lambda i: _last_error() is not None),
        pystray.MenuItem("Dismiss last error", dismiss_error,
                         visible=lambda i: _last_error() is not None),
        pystray.MenuItem("Stop current job", stop_job,
                         visible=lambda i: _job_running()),
        pystray.MenuItem("Stop runner", stop_runner,
                         visible=lambda i: _runner_up()),
        pystray.MenuItem("Open runner folder", open_folder),
        pystray.MenuItem("Exit", _on_exit),
    )
    icon = pystray.Icon(APP_NAME, _make_icon(False), APP_NAME, menu)
    icon.run(setup=_setup)


if __name__ == "__main__":
    main()
