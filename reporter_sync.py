#!/usr/bin/env python3
"""
Fetch A Bola reporter ratings for a zerozero match and store them in Supabase.

Given a zerozero match id, looks up the teams + date, runs abola.py to scrape
the reporter ratings + MVP, and writes:
  * matches_reporter_link  (raw ratings + the A Bola URLs used) — for display
  * match_players.reporter_score / reporter_is_mvp  (best-effort name match)
  * players.abolaid  (when a name matches)
  * a crawl_runs row with source='abola'  (for the reporter runs page)

Portuguese league only. Run on the self-hosted runner (residential IP).
Env: SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (via env or .env).
"""

from __future__ import annotations

import argparse
import os
import sys

import abola
import sync

BIG_THREE = ("benfica", "sporting", "porto")


def _is_big_three(home: str, away: str) -> bool:
    blob = abola._norm(home) + "|" + abola._norm(away)
    return any(k in blob for k in BIG_THREE)


def _fetch_match(sb: sync.Supabase, match_id: str) -> dict | None:
    rows = sb._rest(
        "GET",
        f"matches?id=eq.{match_id}&select=id,played_on,home_team_id,away_team_id,"
        "home:teams!matches_home_team_id_fkey(name),"
        "away:teams!matches_away_team_id_fkey(name)",
    ).json()
    return rows[0] if rows else None


def _link_scores(sb, match_id, team_id, ratings):
    """Best-effort: attach reporter scores to zerozero match_players by name."""
    if not team_id:
        return 0
    players = sb._rest(
        "GET",
        f"match_player_details?match_id=eq.{match_id}&team_id=eq.{team_id}"
        "&select=player_id,player_name",
    ).json()
    by_norm = {abola._norm(p["player_name"]): p["player_id"] for p in players}
    linked = 0
    for r in ratings:
        pid = by_norm.get(abola._norm(r["player_name"]))
        if not pid:
            continue
        sb.update("match_players", f"match_id=eq.{match_id}&player_id=eq.{pid}",
                  {"reporter_score": r["score"],
                   "reporter_is_mvp": bool(r["is_mvp"])})
        if r.get("player_id"):
            sb.update("players", f"id=eq.{pid}", {"abolaid": r["player_id"]})
        linked += 1
    return linked


def run(match_id: str, *, run_id: int | None, github_run_id: str | None,
        delay: float) -> dict:
    sync._load_dotenv()
    sb = sync.Supabase()
    run_id = sync._begin_run(sb, run_id=run_id, trigger="manual", kind="reporter",
                             target=match_id, github_run_id=github_run_id,
                             source="abola")
    try:
        m = _fetch_match(sb, match_id)
        if not m:
            raise RuntimeError(f"match {match_id} not found")
        home = (m.get("home") or {}).get("name")
        away = (m.get("away") or {}).get("name")
        if not home or not away:
            raise RuntimeError("match has no teams yet (crawl it first)")

        match = {"home_team": home, "away_team": away,
                 "game_date": m["played_on"],
                 "is_big_three_match": _is_big_three(home, away)}
        data = abola.scrape_match(match, delay=delay)

        sb.upsert("matches_reporter_link", [{
            "match_id": match_id,
            "format_detected": data["format_detected"],
            "urls": data["urls_used"],
            "home_ratings": data["home_team_ratings"],
            "away_ratings": data["away_team_ratings"],
            "fetched_at": sync._now(),
        }], "match_id")

        linked = _link_scores(sb, match_id, m["home_team_id"], data["home_team_ratings"])
        linked += _link_scores(sb, match_id, m["away_team_id"], data["away_team_ratings"])

        total = len(data["home_team_ratings"]) + len(data["away_team_ratings"])
        sync._finish_run(sb, run_id, status="success", games_count=total)
        print(f"Reporter: {total} ratings ({linked} linked) from "
              f"{len(data['urls_used'])} url(s). crawl_run #{run_id}",
              file=sys.stderr)
        return data
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        print(f"ERROR reporter crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match-id", help="zerozero match id (or env IN_MATCH_ID)")
    ap.add_argument("--run-id", type=int, help="crawl_runs row to update (or IN_RUN_ID)")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    sync._load_dotenv()
    match_id = args.match_id or sync._env("IN_MATCH_ID")
    if not match_id:
        print("No match id (pass --match-id or set IN_MATCH_ID).", file=sys.stderr)
        return 2
    rid_env = sync._env("IN_RUN_ID")
    run_id = args.run_id or (int(rid_env) if rid_env else None)

    run(match_id, run_id=run_id, github_run_id=os.environ.get("GITHUB_RUN_ID"),
        delay=args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
