#!/usr/bin/env python3
"""
Sync crawler output into Supabase, with crawl-run logging.

Runs in GitHub Actions (or locally). It crawls a round or a single match using
crawler.py, then UPSERTs the data into Supabase via the REST (PostgREST) API,
and records the outcome in the `crawl_runs` table so the website can show
live status + history.

Environment:
    SUPABASE_URL                 e.g. https://xxxx.supabase.co
    SUPABASE_SERVICE_ROLE_KEY    service_role secret (bypasses RLS)

Usage:
    python sync.py --jornada 31
    python sync.py --match https://www.zerozero.pt/jogo/.../11071716
    python sync.py --jornada 31 --run-id 42 --trigger manual --github-run-id 123

If --run-id is given (the site pre-created a 'queued' row), that row is updated;
otherwise a new crawl_runs row is created.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

import requests

import crawler

COMPETITION_SLUG = "liga-portuguesa"


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a local .env (gitignored) for local runs.
    Does not override variables already set in the environment (so GitHub
    Actions secrets win, and .env simply doesn't exist there)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(),
                                  val.strip().strip('"').strip("'"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ----------------------------------------------------------------------------
# Supabase REST helpers
# ----------------------------------------------------------------------------


class Supabase:
    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.url or not self.key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
        self.session = requests.Session()
        self.session.headers.update({
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        })

    def _rest(self, method: str, path: str, *, prefer: str | None = None,
              **kw) -> requests.Response:
        headers = {"Prefer": prefer} if prefer else {}
        resp = self.session.request(
            method, f"{self.url}/rest/v1/{path}", headers=headers,
            timeout=30, **kw)
        if not resp.ok:
            raise RuntimeError(
                f"Supabase {method} {path} -> {resp.status_code}: {resp.text}")
        return resp

    def upsert(self, table: str, rows: list[dict], on_conflict: str) -> None:
        if not rows:
            return
        self._rest("POST", f"{table}?on_conflict={on_conflict}",
                   prefer="resolution=merge-duplicates,return=minimal",
                   json=rows)

    def insert_returning(self, table: str, row: dict) -> dict:
        resp = self._rest("POST", table, prefer="return=representation",
                           json=row)
        return resp.json()[0]

    def update(self, table: str, match: str, patch: dict) -> None:
        self._rest("PATCH", f"{table}?{match}", prefer="return=minimal",
                   json=patch)


# ----------------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------------


def _competition_row(comp: dict) -> dict:
    return {
        "id": comp.get("id_edicao"),
        "name": comp.get("name"),
        "slug": COMPETITION_SLUG,
        "fase": comp.get("fase"),
        "updated_at": _now(),
    }


def _collect_entities(games: list[dict]) -> tuple[list[dict], list[dict]]:
    """Dedupe all teams and players that appear across the given games."""
    teams: dict[str, dict] = {}
    players: dict[str, dict] = {}
    now = _now()
    for g in games:
        for tkey in ("home_team", "away_team"):
            t = g.get(tkey)
            if t and t.get("id"):
                teams[t["id"]] = {"id": t["id"], "name": t["name"],
                                  "updated_at": now}
        for p in g.get("players", []):
            if p.get("id"):
                players[p["id"]] = {"id": p["id"], "name": p["name"],
                                    "updated_at": now}
            pt = p.get("team")
            if pt and pt.get("id"):
                teams[pt["id"]] = {"id": pt["id"], "name": pt["name"],
                                   "updated_at": now}
    return list(teams.values()), list(players.values())


def _match_row(game: dict, *, competition_id: str | None, round_no: int | None,
               scraped_at: str | None) -> dict:
    result = game.get("result") or {}
    return {
        "id": game["game_id"],
        "competition_id": competition_id,
        "round": round_no,
        "played_on": game.get("date"),
        "url": game.get("url"),
        "home_team_id": (game.get("home_team") or {}).get("id"),
        "away_team_id": (game.get("away_team") or {}).get("id"),
        "home_score": result.get("home"),
        "away_score": result.get("away"),
        "scraped_at": scraped_at or _now(),
        "updated_at": _now(),
    }


def _match_player_rows(game: dict) -> list[dict]:
    now = _now()
    rows = []
    for p in game.get("players", []):
        s = p["stats"]
        m = p.get("minutes", {})
        rows.append({
            "match_id": game["game_id"],
            "player_id": p["id"],
            "team_id": (p.get("team") or {}).get("id"),
            "shirt_number": p.get("number"),
            "is_captain": p.get("captain", False),
            "is_starter": p.get("starter", False),
            "entered_min": m.get("entered"),
            "left_min": m.get("left"),
            "goals": s["goals"],
            "assists": s["assists"],
            "yellow_cards": s["yellow_cards"],
            "red_card": s["red_card"],
            "own_goals": s["own_goals"],
            "penalties_scored": s["penalties_scored"],
            "penalties_missed": s["penalties_missed"],
            "penalties_defended": s["penalties_defended"],
            "played_under_20m": s["played_under_20m"],
            "updated_at": now,
        })
    return rows


# ----------------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------------


def write_games(sb: Supabase, games: list[dict], *,
                competition: dict | None, round_no: int | None,
                scraped_at: str | None) -> None:
    """Upsert all entities and stats for a set of games (FK-safe order)."""
    games = [g for g in games if g and g.get("game_id")]
    if not games:
        return

    competition_id = None
    if competition:
        sb.upsert("competitions", [_competition_row(competition)], "id")
        competition_id = competition.get("id_edicao")

    teams, players = _collect_entities(games)
    sb.upsert("teams", teams, "id")
    sb.upsert("players", players, "id")

    sb.upsert("matches",
              [_match_row(g, competition_id=competition_id, round_no=round_no,
                          scraped_at=scraped_at) for g in games],
              "id")

    match_players: list[dict] = []
    for g in games:
        match_players.extend(_match_player_rows(g))
    sb.upsert("match_players", match_players, "match_id,player_id")


# ----------------------------------------------------------------------------
# crawl_runs lifecycle + orchestration
# ----------------------------------------------------------------------------


def _begin_run(sb: Supabase, *, run_id: int | None, trigger: str, kind: str,
               target: str, github_run_id: str | None) -> int | None:
    patch = {"status": "running", "started_at": _now(), "kind": kind,
             "target": target, "trigger": trigger,
             "github_run_id": github_run_id}
    if run_id is not None:
        sb.update("crawl_runs", f"id=eq.{run_id}", patch)
        return run_id
    row = sb.insert_returning("crawl_runs", {**patch, "created_at": _now()})
    return row.get("id")


def _finish_run(sb: Supabase, run_id: int | None, *, status: str,
                games_count: int | None = None, error: str | None = None) -> None:
    if run_id is None:
        return
    sb.update("crawl_runs", f"id=eq.{run_id}", {
        "status": status, "finished_at": _now(),
        "games_count": games_count, "error": error,
    })


def run(*, jornada: int | None, match_url: str | None, run_id: int | None,
        trigger: str, github_run_id: str | None, delay: float) -> dict:
    sb = Supabase()
    kind = "match" if match_url else "round"
    target = match_url if match_url else str(jornada if jornada is not None else "current")

    run_id = _begin_run(sb, run_id=run_id, trigger=trigger, kind=kind,
                        target=target, github_run_id=github_run_id)
    try:
        comp = crawler.get_competition(delay=delay)
        if match_url:
            game = crawler.get_match(match_url, delay=delay)
            games = [game]
            round_no = None
            scraped_at = _now()
        else:
            data = crawler.crawl_round(jornada=jornada, competition=comp,
                                       delay=delay)
            games = data["games"]
            round_no = data["round"]
            scraped_at = data.get("scraped_at")

        write_games(sb, games, competition=comp, round_no=round_no,
                    scraped_at=scraped_at)
        _finish_run(sb, run_id, status="success", games_count=len(games))
        print(f"Synced {len(games)} game(s). crawl_run #{run_id}",
              file=sys.stderr)
        return {"run_id": run_id, "games": len(games)}
    except Exception as err:  # noqa: BLE001
        _finish_run(sb, run_id, status="error", error=str(err)[:2000])
        print(f"ERROR in crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def _env(*names: str) -> str | None:
    """First non-empty environment variable among names."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


def main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--jornada", type=int,
                      help="Round to crawl & sync (defaults to current round).")
    mode.add_argument("--match", help="Single match URL to crawl & sync.")
    ap.add_argument("--run-id", type=int,
                    help="Existing crawl_runs row to update (else a new one is created).")
    ap.add_argument("--trigger", default="manual", choices=["manual", "schedule"],
                    help="How this run was triggered (for the log).")
    ap.add_argument("--github-run-id", default=os.environ.get("GITHUB_RUN_ID"),
                    help="GitHub Actions run id, stored for linking.")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Polite delay between zerozero requests.")
    args = ap.parse_args()

    # CLI flags win; otherwise fall back to env vars (set by the workflow, so
    # the workflow step needs no shell-specific conditionals).
    jornada = args.jornada
    match_url = args.match
    if jornada is None and match_url is None:
        match_url = _env("IN_MATCH")
        if not match_url:
            jor = _env("IN_JORNADA")
            jornada = int(jor) if jor else None

    run_id = args.run_id
    if run_id is None:
        rid = _env("IN_RUN_ID")
        run_id = int(rid) if rid else None

    trigger = args.trigger
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        trigger = "schedule"

    run(jornada=jornada, match_url=match_url, run_id=run_id,
        trigger=trigger, github_run_id=args.github_run_id, delay=args.delay)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
