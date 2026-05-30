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


# Full-time markers seen in the feed's minute field (refined from live data).
FINISHED_RE = re.compile(r"\b(FIM|FINAL|TERMINAD|FT|AP|PEN|GP)\b", re.I)


def _is_finished(raw) -> bool:
    return bool(FINISHED_RE.search(str(raw or "")))


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


def light_update(sb, gid, *, minute, gc, gf):
    sb.update("matches", f"id=eq.{gid}", {
        "status": "live", "minute": minute or None,
        "home_score": gc, "away_score": gf,
        "scraped_at": sync._now(), "updated_at": sync._now(),
    })


def watch_loop(sb, session, match_url, *, light_interval, full_interval,
               max_minutes):
    gid = game_id_from(match_url)
    referer = match_url
    print(f"Watching match {gid}: {match_url}", file=sys.stderr)

    full_crawl(sb, session, match_url, status="scheduled")  # seed lineup

    last_score = None
    last_full = time.time()
    started = time.time()

    while True:
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
            raw_minute = _field(row, 2)
            result = str(_field(row, 1) or "")
            minute = _fmt_minute(raw_minute)
            gc, gf = _int(_field(row, 3)), _int(_field(row, 4))
            if result:  # live (has a score line)
                score = (gc, gf)
                light_update(sb, gid, minute=minute, gc=gc, gf=gf)
                score_changed = score != last_score and last_score is not None
                routine_due = (time.time() - last_full) >= full_interval
                if score_changed or routine_due:
                    full_crawl(sb, session, match_url, status="live",
                               minute=minute, score=score)
                    last_full = time.time()
                last_score = score
                if _is_finished(raw_minute):
                    print(f"Full-time marker ({raw_minute}) — finalizing.",
                          file=sys.stderr)
                    break

        time.sleep(light_interval)

    full_crawl(sb, session, match_url, status="final",
               score=last_score or None, minute=None)
    print("Done. Match marked final.", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", help="zerozero match URL (or env IN_MATCH)")
    ap.add_argument("--run-id", type=int, help="crawl_runs row to update (or IN_RUN_ID)")
    ap.add_argument("--light-interval", type=float, default=30)
    ap.add_argument("--full-interval", type=float, default=120)
    ap.add_argument("--max-minutes", type=float, default=200)
    args = ap.parse_args()

    sync._load_dotenv()
    match_url = args.match or sync._env("IN_MATCH")
    if not match_url:
        print("No match URL (pass --match or set IN_MATCH).", file=sys.stderr)
        return 2
    rid_env = sync._env("IN_RUN_ID")
    run_id = args.run_id or (int(rid_env) if rid_env else None)

    sb = sync.Supabase()
    session = crawler.new_session()

    run_id = sync._begin_run(sb, run_id=run_id, trigger="manual", kind="watch",
                             target=match_url,
                             github_run_id=os.environ.get("GITHUB_RUN_ID"))
    try:
        watch_loop(sb, session, match_url,
                   light_interval=args.light_interval,
                   full_interval=args.full_interval,
                   max_minutes=args.max_minutes)
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
