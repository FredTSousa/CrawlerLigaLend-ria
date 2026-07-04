#!/usr/bin/env python3
"""
Live watcher for an ongoing match — gentle on zerozero.

Instead of re-fetching the full match page repeatedly, this polls the same
lightweight endpoint the site's own JavaScript uses:

    GET /match_live_update.php?ids=<gameId>&page=
    -> [[id, result, minute, home_goals, away_goals, state], ...]

every ~30s (matching the browser). It updates the live score/minute/status in
Supabase cheaply, and only does a FULL page crawl (player stats) when the score
changes, on a slow cadence, and once at the end.

Run on the machine with the residential IP (where Crawler LLP runs):

    python live_watch.py --match https://www.zerozero.pt/jogo/.../12083086

Needs SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (via env or .env, like sync.py).
Stop with Ctrl+C — it does a final full crawl and marks the match final.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import crawler
import sync

LIVE_ENDPOINT = "https://www.zerozero.pt/match_live_update.php"


def game_id_from(url: str) -> str:
    m = re.search(r"/(\d+)(?:[/?#]|$)", url)
    return m.group(1) if m else url


def _int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


# The feed returns objects keyed "0".."5":
#   "0"=id  "1"=result("gc-gf")  "2"=minute  "3"=home goals  "4"=away goals  "5"=state
def _field(row, i):
    if isinstance(row, dict):
        return row.get(str(i))
    return row[i] if i < len(row) else None


def poll_light(session, gid: str, referer: str):
    """Return the feed row (dict/list) for gid, or None if not present."""
    r = session.get(
        f"{LIVE_ENDPOINT}?ids={gid}&page=",
        headers={
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=20,
    )
    r.raise_for_status()
    try:
        data = r.json()
    except Exception:
        data = json.loads(r.text or "[]")
    for row in data:
        if str(_field(row, 0)) == str(gid):
            return row
    return None


def _fmt_minute(m) -> str | None:
    s = str(m).strip() if m is not None else ""
    if not s:
        return None
    if s.upper() in ("INT", "INTERVALO"):
        return "HT"
    return f"{s}'" if re.fullmatch(r"\d+(\+\d+)?", s) else s


def _is_finished_row(row) -> bool:
    """The feed's state field (index 5): "0" = live, "1" = finished/not-live.
    This is the reliable end signal — holds for normal time, extra time and
    penalties (the minute field just goes empty)."""
    return str(_field(row, 5) or "") == "1"


# Status file the tray reads to show which matches are being watched.
STATUS_FILE = os.path.join(
    os.environ.get("USERPROFILE", "."), "actions-runner", "_watch_status.json")


def write_status(items: list[dict]) -> None:
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"updated": time.time(), "matches": items}, fh)
    except Exception:  # noqa: BLE001
        pass


def poll_multi(session, ids: str, referer: str) -> dict:
    """Poll the live endpoint for several ids at once; return {id: row}."""
    r = session.get(
        f"{LIVE_ENDPOINT}?ids={ids}&page=",
        headers={
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=20,
    )
    r.raise_for_status()
    try:
        data = r.json()
    except Exception:
        data = json.loads(r.text or "[]")
    return {str(_field(row, 0)): row for row in data}


def full_crawl(sb, session, url, *, status, minute=None, score=None):
    """Fetch the full match page and upsert players + match (heavier call)."""
    game = crawler.get_match(url, session=session)
    if score is not None:
        game["result"] = {"home": score[0], "away": score[1]}
    game["status"] = status
    game["minute"] = minute
    sync.write_games(sb, [game], competition=None, round_no=None,
                     scraped_at=sync._now())
    n = len(game.get("players", []))
    print(f"  full crawl: status={status} score={score} minute={minute} "
          f"players={n}", file=sys.stderr)
    return game


def phase_followup(sb, gid):
    """After a KNOCKOUT match goes final, queue a fixtures backfill for its
    competition so the next round's pairings (which zerozero fills in as
    results land) reach the DB — and subscribers — minutes after the whistle
    instead of waiting for the 6-hourly sweep. Gated on matches.phase, which
    only knockout crawls set, so a league match finishing enqueues nothing.
    Same job kind + dedupe key as the website's backfill button, so repeated
    finals (or a click racing this) collapse into one pending job.
    Best-effort: the sweep's phase look-ahead covers any miss here."""
    try:
        rows = sb._rest(
            "GET", f"matches?id=eq.{gid}&select=phase,competition_id").json()
        row = rows[0] if rows else {}
        comp_id = row.get("competition_id")
        if not row.get("phase") or not comp_id:
            return
        comps = sb._rest(
            "GET", f"competitions?id=eq.{comp_id}&select=slug").json()
        comp = (comps[0].get("slug") if comps else None) or comp_id
        sb.enqueue_job("backfill", {"competition": comp},
                       dedupe_key=f"backfill:{comp}", priority=90)
        print(f"  knockout final — queued fixtures backfill for {comp}",
              file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - never let this kill the watcher
        print(f"  ! phase follow-up enqueue failed for {gid}: {e}",
              file=sys.stderr)


def light_update(sb, gid, *, minute, gc, gf):
    sb.update("matches", f"id=eq.{gid}", {
        "status": "live", "minute": minute or None,
        "home_score": gc, "away_score": gf,
        "scraped_at": sync._now(), "updated_at": sync._now(),
    })


def heartbeat(sb, run_id):
    """Bump crawl_runs.last_seen_at so the kickoff cron can tell this daemon is
    alive and must NOT re-dispatch (which would cancel it). Best-effort: a failed
    beat is logged, not fatal — but if it keeps failing the cron will eventually
    treat the watcher as dead and restart it."""
    if run_id is None:
        return
    try:
        sb.update("crawl_runs", f"id=eq.{run_id}", {"last_seen_at": sync._now()})
    except Exception as e:  # noqa: BLE001
        print(f"  heartbeat failed: {e}", file=sys.stderr)


def watch_loop(sb, session, match_url, *, light_interval, full_interval,
               max_minutes, run_id=None):
    gid = game_id_from(match_url)
    referer = match_url
    print(f"Watching match {gid}: {match_url}", file=sys.stderr)

    full_crawl(sb, session, match_url, status="scheduled")  # seed lineup

    last_score = None
    last_full = time.time()
    started = time.time()

    while True:
        heartbeat(sb, run_id)
        if (time.time() - started) / 60 > max_minutes:
            print("Max watch time reached — finalizing.", file=sys.stderr)
            break
        try:
            row = poll_light(session, gid, referer)
        except Exception as e:  # noqa: BLE001 - keep watching on transient errors
            print(f"  poll error: {e}", file=sys.stderr)
            time.sleep(light_interval)
            continue

        print(f"[{time.strftime('%H:%M:%S')}] feed: {row}", file=sys.stderr)

        if row:
            result = str(_field(row, 1) or "")
            minute = _fmt_minute(_field(row, 2))
            gc, gf = _int(_field(row, 3)), _int(_field(row, 4))
            finished = _is_finished_row(row)
            if result and finished:
                print("Full-time (state=1) — finalizing.", file=sys.stderr)
                last_score = (gc, gf)
                break
            if result:  # live
                score = (gc, gf)
                light_update(sb, gid, minute=minute, gc=gc, gf=gf)
                score_changed = score != last_score and last_score is not None
                routine_due = (time.time() - last_full) >= full_interval
                if score_changed or routine_due:
                    full_crawl(sb, session, match_url, status="live",
                               minute=minute, score=score)
                    last_full = time.time()
                last_score = score

        time.sleep(light_interval)

    full_crawl(sb, session, match_url, status="final",
               score=last_score or None, minute=None)
    phase_followup(sb, gid)
    print("Done. Match marked final.", file=sys.stderr)


def daemon_loop(sb, session, *, light_interval, full_interval, max_minutes,
                idle_exit_cycles=6, run_id=None):
    """Follow every match flagged watch=true in the DB, all in one poll.
    Exits when nothing is left to watch (frees the runner)."""
    referer = "https://www.zerozero.pt/"
    state: dict[str, dict] = {}   # gid -> {last_score, last_full}
    started = time.time()
    idle = 0

    while True:
        heartbeat(sb, run_id)
        if (time.time() - started) / 60 > max_minutes:
            print("Max watch time reached — stopping daemon.", file=sys.stderr)
            break

        try:
            targets = sb.watch_list()  # [{id,url}]
        except Exception as e:  # noqa: BLE001
            print(f"  watch_list error: {e}", file=sys.stderr)
            time.sleep(light_interval)
            continue

        if not targets:
            write_status([])
            idle += 1
            if idle >= idle_exit_cycles:
                print("No matches to watch — daemon idle, exiting.",
                      file=sys.stderr)
                break
            time.sleep(light_interval)
            continue
        idle = 0

        ids = "|".join(t["id"] for t in targets)
        try:
            rows = poll_multi(session, ids, referer)
        except Exception as e:  # noqa: BLE001
            print(f"  poll error: {e}", file=sys.stderr)
            time.sleep(light_interval)
            continue

        statuses = []
        for t in targets:
            gid, url = t["id"], t["url"]
            row = rows.get(str(gid))
            st = state.setdefault(gid, {"last_score": None, "last_full": 0.0})
            if not row:
                # Feed has no data yet (pre-kickoff or delayed). Seed the lineup
                # on first encounter and keep the match page fresh every
                # full_interval so postponements/delays are caught early.
                no_feed_due = (time.time() - st["last_full"]) >= full_interval
                if no_feed_due:
                    try:
                        full_crawl(sb, session, url, status="scheduled")
                        st["last_full"] = time.time()
                    except Exception as e:  # noqa: BLE001
                        print(f"  pre-kickoff crawl error for {gid}: {e}",
                              file=sys.stderr)
                continue
            result = str(_field(row, 1) or "")
            minute = _fmt_minute(_field(row, 2))
            gc, gf = _int(_field(row, 3)), _int(_field(row, 4))
            if not result:
                continue
            score = (gc, gf)
            if _is_finished_row(row):  # state=1 -> finished (incl. ET/pens)
                print(f"Full-time (state=1) for {gid} — finalizing.",
                      file=sys.stderr)
                full_crawl(sb, session, url, status="final", score=score)
                sb.set_watch(gid, False)
                phase_followup(sb, gid)
                continue
            light_update(sb, gid, minute=minute, gc=gc, gf=gf)
            changed = score != st["last_score"] and st["last_score"] is not None
            routine = (time.time() - st["last_full"]) >= full_interval
            if changed or routine:
                full_crawl(sb, session, url, status="live", minute=minute,
                           score=score)
                st["last_full"] = time.time()
            st["last_score"] = score
            statuses.append({"id": gid, "url": url, "minute": minute,
                             "score": f"{gc}-{gf}"})

        print(f"[{time.strftime('%H:%M:%S')}] watching {len(statuses)}: "
              f"{statuses}", file=sys.stderr)
        write_status(statuses)
        time.sleep(light_interval)

    write_status([])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", help="single match URL (or env IN_MATCH)")
    ap.add_argument("--daemon", action="store_true",
                    help="follow all matches flagged watch=true in the DB")
    ap.add_argument("--run-id", type=int, help="crawl_runs row to update (or IN_RUN_ID)")
    ap.add_argument("--light-interval", type=float, default=30)
    ap.add_argument("--full-interval", type=float, default=120)
    ap.add_argument("--max-minutes", type=float, default=200)
    args = ap.parse_args()

    sync._load_dotenv()
    rid_env = sync._env("IN_RUN_ID")
    run_id = args.run_id or (int(rid_env) if rid_env else None)
    sb = sync.Supabase()
    session = crawler.new_session()

    daemon = args.daemon or sync._env("WATCH_DAEMON")
    if daemon:
        run_id = sync._begin_run(sb, run_id=run_id, trigger="manual",
                                 kind="watch", target="watch-list",
                                 github_run_id=os.environ.get("GITHUB_RUN_ID"))
        try:
            daemon_loop(sb, session, light_interval=args.light_interval,
                        full_interval=args.full_interval,
                        max_minutes=args.max_minutes, run_id=run_id)
            sync._finish_run(sb, run_id, status="success")
        except KeyboardInterrupt:
            print("\nInterrupted.", file=sys.stderr)
            sync._finish_run(sb, run_id, status="success")
        except Exception as err:  # noqa: BLE001
            sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
            raise
        return 0

    # Single-match mode (manual / local use).
    match_url = args.match or sync._env("IN_MATCH")
    if not match_url:
        print("No match URL (pass --match, --daemon, or set IN_MATCH).",
              file=sys.stderr)
        return 2
    run_id = sync._begin_run(sb, run_id=run_id, trigger="manual", kind="watch",
                             target=match_url,
                             github_run_id=os.environ.get("GITHUB_RUN_ID"))
    try:
        watch_loop(sb, session, match_url,
                   light_interval=args.light_interval,
                   full_interval=args.full_interval,
                   max_minutes=args.max_minutes, run_id=run_id)
        sync._finish_run(sb, run_id, status="success", games_count=1)
    except KeyboardInterrupt:
        print("\nInterrupted — finalizing.", file=sys.stderr)
        try:
            full_crawl(sb, session, match_url, status="final")
        except Exception:  # noqa: BLE001
            pass
        sync._finish_run(sb, run_id, status="success", games_count=1)
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
