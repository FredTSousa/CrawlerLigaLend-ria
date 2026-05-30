# Crawler LLP — runner tray app

`runner_tray.py` is a Windows system-tray controller for the self-hosted
GitHub Actions runner that crawls zerozero.pt. It exists because zerozero
blocks datacenter IPs, so the crawl jobs must run on this machine's home IP.

## What it does
- Sits in the system tray as **Crawler LLP**.
- **Green** icon = the runner is up (listening for / running jobs); **grey** = stopped.
- Right-click menu: **Start runner**, **Stop runner**, **Open runner folder**, **Exit**.
- On launch it auto-starts the runner; on Exit it stops it.

It manages the runner installed at `%USERPROFILE%\actions-runner` (`run.cmd`).

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
