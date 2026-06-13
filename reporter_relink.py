#!/usr/bin/env python3
"""
Re-scrape one match's A Bola reporter ratings from EXPLICIT URL(s) and overwrite
what's stored.

Use this when the crawler attached the wrong A Bola page (e.g. a club that played
twice inside the date window got the *other* game's "as notas" page) and you know
the right URL. The normal reporter_sync re-searches A Bola every run, so it can't
be pointed at a specific page -- and for an old game the correct, creatively-titled
page may no longer be reachable by search/feed at all. This bypasses discovery:
you give the URL, it scrapes + re-stores + re-links exactly like a normal run.

It reuses reporter_sync._store_and_link, so it is idempotent and manual-safe:
prior AUTO links are reset and replaced; rows a human linked (reporter_manual)
keep their score. matches_reporter_link is overwritten for the match.

  python reporter_relink.py --match-id <zz_id> --home-url <abola_url>
  python reporter_relink.py --match-id <zz_id> --home-url <url> --away-url <url>
  python reporter_relink.py --match-id <zz_id> --single-url <cronica_url>

--home-url / --away-url feed the big-three two-page format (one "as notas do X"
per team); --single-url feeds a combined crónica. A side left unspecified is
discovered normally (now opponent-aware), so for a big-three game you usually only
need to pin the one page that was wrong. Env: SUPABASE_URL +
SUPABASE_SERVICE_ROLE_KEY (via env or .env), same as reporter_sync.
"""

from __future__ import annotations

import argparse
import sys

import abola
import reporter_sync
import sync


def relink(match_id: str, *, home_url: str | None = None,
           away_url: str | None = None, single_url: str | None = None,
           delay: float = 1.5) -> dict:
    sync._load_dotenv()
    sb = sync.Supabase()
    reporter_sync._load_team_aliases(sb)

    m = reporter_sync._fetch_match(sb, match_id)
    if not m:
        raise RuntimeError(f"match {match_id} not found")
    home = (m.get("home") or {}).get("name")
    away = (m.get("away") or {}).get("name")
    if not home or not away:
        raise RuntimeError("match has no teams yet (crawl it first)")

    match = {"home_team": home, "away_team": away, "game_date": m["played_on"],
             "is_big_three_match": reporter_sync._is_big_three(home, away)}
    data = abola.scrape_match(match, single_url=single_url,
                              home_url=home_url, away_url=away_url, delay=delay)

    linked, total = reporter_sync._store_and_link(
        sb, match_id, m["home_team_id"], m["away_team_id"], data)
    print(f"Relinked {home} vs {away} ({m['played_on']}): {total} ratings "
          f"({linked} linked) from {len(data['urls_used'])} url(s): "
          f"{', '.join(data['urls_used'])}", file=sys.stderr)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match-id", required=True, help="zerozero match id")
    ap.add_argument("--home-url", help="A Bola 'as notas do <home>' page")
    ap.add_argument("--away-url", help="A Bola 'as notas do <away>' page")
    ap.add_argument("--single-url", help="A Bola combined crónica page")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    if not (args.home_url or args.away_url or args.single_url):
        print("Give at least one of --home-url / --away-url / --single-url.",
              file=sys.stderr)
        return 2

    relink(args.match_id, home_url=args.home_url, away_url=args.away_url,
           single_url=args.single_url, delay=args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
