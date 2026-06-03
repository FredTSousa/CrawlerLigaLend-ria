# Crawler LLP — runner tray app

`runner_tray.py` is a Windows system-tray controller for the self-hosted
GitHub Actions runner that crawls zerozero.pt. It exists because zerozero
blocks datacenter IPs, so the crawl jobs must run on this machine's home IP.

## What it does
- Sits in the system tray as **Crawler LLP**.
- Icon colour: **grey** = stopped, **green** = up & idle, **blue** = a job (or
  live watch) is running, **red** = idle but the *last* job failed.
- Hover the icon (or open the menu) to see what it's doing. While a job runs it
  shows the live stage, progress, and how long since the last update, e.g.
  `Squads: Transfermarkt: Sporting CP [12/18] · 4s ago` — if the "… ago" keeps
  climbing, the job is stalled; if it ticks, it's moving. When idle it shows the
  last run's outcome, e.g. `Last Squads: success — Synced 18 team(s)…` or
  `Last League matches: error — …`.
- On a failure the icon goes red and a **Show last error…** menu item appears
  that pops up the full error text, so you don't have to open the website.
- Right-click menu: **Start runner**, **Stop current job**, **Stop runner**,
  **Open runner folder**, **Exit**.
- On launch it auto-starts the runner; on Exit it stops it.

It manages the runner installed at `%USERPROFILE%\actions-runner` (`run.cmd`).
The live detail comes from `_job_status.json` in that folder, written by the
jobs via `jobstatus.py` (and `_watch_status.json` for live watches).

> After updating the repo, **Exit and relaunch** the tray so it picks up the new
> version — the running instance keeps the old behaviour until restarted.

## Requirements
```
pip install -r tools/requirements.txt   # pystray, pillow
```

## Running it
- Double-click the **Crawler LLP** shortcut on the Desktop, or run:
  ```
  pythonw tools\runner_tray.py
  ```
  (`pythonw` = no console window.)

When the icon is green, the website's "Crawl" buttons (and the 6-hour
schedule) can run. When you Exit it, crawls won't run until you start it again.

## Optional: launch automatically at login
Not enabled by default. To turn it on, put a shortcut to the same command in
your Startup folder (Win+R → `shell:startup`), targeting:
`pythonw.exe  "F:\GitHub\CrawlerLigaLend-ria\tools\runner_tray.py"`.
