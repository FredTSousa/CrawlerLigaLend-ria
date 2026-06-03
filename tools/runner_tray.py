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
import json
import os
import re
import subprocess
import threading
import time

import pystray
from PIL import Image, ImageDraw

APP_NAME = "Crawler LLP"
RUNNER_DIR = os.path.join(os.environ.get("USERPROFILE", ""), "actions-runner")
RUN_CMD = os.path.join(RUNNER_DIR, "run.cmd")
LOG_FILE = os.path.join(RUNNER_DIR, "_tray_run.log")
WATCH_STATUS = os.path.join(RUNNER_DIR, "_watch_status.json")
JOB_STATUS = os.path.join(RUNNER_DIR, "_job_status.json")  # written by jobstatus.py
LISTENER = "Runner.Listener.exe"
WORKER = "Runner.Worker.exe"  # exists only while a job (e.g. a live watch) runs

# Windows: don't pop up console windows for our helper processes.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

GREEN = (40, 180, 80, 255)
BLUE = (60, 140, 240, 255)
GREY = (120, 120, 120, 255)
RED = (210, 60, 60, 255)

_proc: subprocess.Popen | None = None


# ----------------------------------------------------------------------------
# Runner state + control
# ----------------------------------------------------------------------------


def _proc_running(image: str) -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, creationflags=_NO_WINDOW).stdout
        return image.lower() in out.lower()
    except Exception:
        return False


def _runner_up() -> bool:
    """True if the runner listener process is alive (started by us or not)."""
    if _proc_running(LISTENER):
        return True
    return _proc is not None and _proc.poll() is None


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


def _job_status() -> dict | None:
    """The crawl/roster/reporter job heartbeat (written by jobstatus.py)."""
    try:
        with open(JOB_STATUS, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


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
    """Full text of the last job's error, if the last finished job failed."""
    js = _job_status()
    if js and js.get("phase") == "done" and js.get("result") == "error":
        return js.get("message") or "(no detail recorded)"
    return None


def show_last_error(icon=None, item=None) -> None:
    msg = _last_error()
    if not msg:
        return
    try:  # MB_OK | MB_ICONERROR | MB_TOPMOST | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(
            0, msg, f"{APP_NAME} — last job error", 0x10 | 0x40000 | 0x10000)
    except Exception:
        pass


def dismiss_error(icon=None, item=None) -> None:
    """Clear the last-error indicator (red icon + status). The next job will
    write a fresh status anyway; this just acknowledges the current one."""
    try:
        if os.path.exists(JOB_STATUS):
            os.remove(JOB_STATUS)
    except Exception:
        pass
    if icon is not None:
        icon.update_menu()


def stop_job(icon=None, item=None) -> None:
    """Cancel the current job (kills the worker) but keep the runner online."""
    subprocess.run(["taskkill", "/F", "/T", "/IM", WORKER],
                   creationflags=_NO_WINDOW, capture_output=True)


def start_runner(icon=None, item=None) -> None:
    global _proc
    if _runner_up() or not os.path.exists(RUN_CMD):
        return
    log = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
    _proc = subprocess.Popen(
        ["cmd", "/c", RUN_CMD], cwd=RUNNER_DIR,
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        creationflags=_NO_WINDOW)


def stop_runner(icon=None, item=None) -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(_proc.pid)],
                       creationflags=_NO_WINDOW,
                       capture_output=True)
    # Also clear any listener started outside this app.
    subprocess.run(["taskkill", "/F", "/T", "/IM", LISTENER],
                   creationflags=_NO_WINDOW, capture_output=True)
    _proc = None


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
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start runner", start_runner,
                         visible=lambda i: not _runner_up()),
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
