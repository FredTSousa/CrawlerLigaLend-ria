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

import os
import subprocess
import threading
import time

import pystray
from PIL import Image, ImageDraw

APP_NAME = "Crawler LLP"
RUNNER_DIR = os.path.join(os.environ.get("USERPROFILE", ""), "actions-runner")
RUN_CMD = os.path.join(RUNNER_DIR, "run.cmd")
LOG_FILE = os.path.join(RUNNER_DIR, "_tray_run.log")
LISTENER = "Runner.Listener.exe"

# Windows: don't pop up console windows for our helper processes.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

GREEN = (40, 180, 80, 255)
GREY = (120, 120, 120, 255)

_proc: subprocess.Popen | None = None


# ----------------------------------------------------------------------------
# Runner state + control
# ----------------------------------------------------------------------------


def _runner_up() -> bool:
    """True if the runner listener process is alive (started by us or not)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {LISTENER}", "/NH"],
            capture_output=True, text=True, creationflags=_NO_WINDOW).stdout
        return LISTENER.lower() in out.lower()
    except Exception:
        return _proc is not None and _proc.poll() is None


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


def _make_icon(up: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=GREEN if up else GREY,
              outline=(255, 255, 255, 255), width=3)
    # A small "C" so it reads as Crawler LLP at a glance.
    d.text((22, 18), "C", fill=(255, 255, 255, 255))
    return img


def _status_text(_=None) -> str:
    return f"Status: {'Running' if _runner_up() else 'Stopped'}"


def _on_exit(icon, item) -> None:
    stop_runner(icon)
    icon.stop()


def _monitor(icon: pystray.Icon) -> None:
    last = None
    while True:
        up = _runner_up()
        if up != last:
            icon.icon = _make_icon(up)
            icon.title = f"{APP_NAME} — {'Running' if up else 'Stopped'}"
            icon.update_menu()
            last = up
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
        pystray.MenuItem("Stop runner", stop_runner,
                         visible=lambda i: _runner_up()),
        pystray.MenuItem("Open runner folder", open_folder),
        pystray.MenuItem("Exit", _on_exit),
    )
    icon = pystray.Icon(APP_NAME, _make_icon(False), APP_NAME, menu)
    icon.run(setup=_setup)


if __name__ == "__main__":
    main()
