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
    return f"{s}'" if re.fullmatch(r"\d+(\+\d+)?", s) else s


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match", required=True, help="zerozero match URL")
    ap.add_argument("--light-interval", type=float, default=30,
                    help="seconds between cheap live-endpoint polls")
    ap.add_argument("--full-interval", type=float, default=300,
                    help="seconds between routine full crawls (cards/subs)")
    ap.add_argument("--max-minutes", type=float, default=180,
                    help="safety cap; finalize and stop after this long")
    args = ap.parse_args()

    sync._load_dotenv()
    sb = sync.Supabase()
    session = crawler.new_session()
    gid = game_id_from(args.match)
    referer = args.match
    print(f"Watching match {gid}: {args.match}", file=sys.stderr)

    # Seed teams/lineup once up front.
    full_crawl(sb, session, args.match, status="scheduled")

    last_score = None
    last_full = time.time()
    started = time.time()
    seen_live = False

    try:
        while True:
            elapsed_min = (time.time() - started) / 60
            if elapsed_min > args.max_minutes:
                print("Max watch time reached — finalizing.", file=sys.stderr)
                break

            try:
                row = poll_light(session, gid, referer)
            except Exception as e:  # noqa: BLE001 - keep watching on transient errors
                print(f"  poll error: {e}", file=sys.stderr)
                time.sleep(args.light_interval)
                continue

            # Log the raw feed so we can learn the 'minute'/'state' semantics.
            print(f"[{time.strftime('%H:%M:%S')}] feed: {row}", file=sys.stderr)

            if row:
                result = str(_field(row, 1) or "")
                minute = _fmt_minute(_field(row, 2))
                gc, gf = _int(_field(row, 3)), _int(_field(row, 4))
                if result:  # match is live (has a score line)
                    seen_live = True
                    score = (gc, gf)
                    light_update(sb, gid, minute=minute, gc=gc, gf=gf)

                    score_changed = score != last_score and last_score is not None
                    routine_due = (time.time() - last_full) >= args.full_interval
                    if score_changed or routine_due:
                        full_crawl(sb, session, args.match, status="live",
                                   minute=minute, score=score)
                        last_full = time.time()
                    last_score = score

            time.sleep(args.light_interval)
    except KeyboardInterrupt:
        print("\nStopping — finalizing match.", file=sys.stderr)

    # Final full crawl; mark final.
    score = last_score if last_score else None
    full_crawl(sb, session, args.match, status="final",
               score=score, minute=None)
    print("Done. Match marked final.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
