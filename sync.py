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
    python sync.py --competition la-liga --jornada 5
    python sync.py --competition la-liga --backfill        # all not-yet-final rounds
    python sync.py --competition la-liga --backfill --force # every round

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
import jobstatus  # best-effort progress heartbeat for the runner tray

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

    def upsert_partial(self, table: str, rows: list[dict], on_conflict: str, *,
                       chunk: int = 50) -> tuple[int, list[tuple[dict, str]]]:
        """Like upsert(), but never lets one bad row sink the whole batch. Tries
        the bulk write first (fast path); on failure it splits into chunks and
        then single rows, so every good row still persists. Returns
        (saved_count, failures) where failures is [(row, error_message), ...]."""
        if not rows:
            return (0, [])
        try:
            self.upsert(table, rows, on_conflict)
            return (len(rows), [])
        except Exception:  # noqa: BLE001 - fall back to isolating the bad rows
            pass
        saved = 0
        failures: list[tuple[dict, str]] = []
        for i in range(0, len(rows), chunk):
            part = rows[i:i + chunk]
            try:
                self.upsert(table, part, on_conflict)
                saved += len(part)
            except Exception:  # noqa: BLE001 - narrow down to the offending rows
                for r in part:
                    try:
                        self.upsert(table, [r], on_conflict)
                        saved += 1
                    except Exception as err:  # noqa: BLE001
                        failures.append((r, str(err)))
        return (saved, failures)

    def insert_returning(self, table: str, row: dict) -> dict:
        resp = self._rest("POST", table, prefer="return=representation",
                           json=row)
        return resp.json()[0]

    def update(self, table: str, match: str, patch: dict) -> None:
        self._rest("PATCH", f"{table}?{match}", prefer="return=minimal",
                   json=patch)

    def match_existing(self, ids: list[str]) -> dict[str, dict]:
        """Current status + competition_id of the given match ids, so a crawl can
        keep status moving forward only and never null out an already-known
        league (live/single-match crawls don't carry a competition)."""
        if not ids:
            return {}
        inlist = ",".join(ids)
        resp = self._rest("GET",
                          f"matches?id=in.({inlist})&select=id,status,competition_id")
        return {r["id"]: r for r in resp.json()}

    def competition_rounds_status(self, competition_id: str) -> dict[int, list[str]]:
        """Map each crawled round of a competition to its games' statuses, so a
        backfill can skip rounds that are already fully 'final' and (re)crawl
        rounds that are missing or still have scheduled/live games."""
        if not competition_id:
            return {}
        resp = self._rest(
            "GET", f"matches?competition_id=eq.{competition_id}&select=round,status")
        out: dict[int, list[str]] = {}
        for r in resp.json():
            rd = r.get("round")
            if rd is None:
                continue
            out.setdefault(rd, []).append(r.get("status"))
        return out

    def watch_list(self) -> list[dict]:
        """Matches flagged for live watching that aren't finished yet."""
        resp = self._rest(
            "GET", "matches?watch=is.true&status=neq.final&select=id,url")
        return resp.json()

    def set_watch(self, match_id: str, on: bool) -> None:
        self.update("matches", f"id=eq.{match_id}", {"watch": on})

    def unscored_finished_matches(self, competition_id: str) -> list[str]:
        """Match IDs in this competition that are final but have at least one
        player with reporter_linked=false (reporter scores not yet fetched)."""
        resp = self._rest(
            "GET",
            f"match_players?select=match_id,matches!inner(competition_id,status)"
            f"&matches.competition_id=eq.{competition_id}"
            f"&matches.status=eq.final"
            f"&reporter_linked=eq.false",
        )
        rows = resp.json()
        seen: set[str] = set()
        out: list[str] = []
        for r in rows:
            mid = r.get("match_id")
            if mid and mid not in seen:
                seen.add(mid)
                out.append(mid)
        return out


# ----------------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------------


def _competition_row(comp: dict) -> dict:
    return {
        "id": comp.get("id_edicao"),
        "name": comp.get("name"),
        # Derive the slug from the crawled competition; fall back to the Liga
        # default for legacy single-match/round runs that don't carry one.
        "slug": comp.get("slug") or COMPETITION_SLUG,
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
               scraped_at: str | None, phase: str | None = None) -> dict:
    result = game.get("result") or {}
    has_score = result.get("home") is not None and result.get("away") is not None

    # A round (fixture) crawl only carries a score for matches that have been
    # played, so a scored fixture is final. Direct match-URL crawls default to
    # the crawler's status (currently 'scheduled' until live detection lands).
    status = game.get("status") or "scheduled"
    if round_no is not None and has_score:
        status = "final"

    row = {
        "id": game["game_id"],
        "competition_id": competition_id,
        "round": round_no,
        "played_on": game.get("date"),
        "url": game.get("url"),
        "home_team_id": (game.get("home_team") or {}).get("id"),
        "away_team_id": (game.get("away_team") or {}).get("id"),
        "home_score": result.get("home"),
        "away_score": result.get("away"),
        "status": status,
        "minute": game.get("minute"),
        "kickoff_at": game.get("kickoff_at"),
        "scraped_at": scraped_at or _now(),
        "updated_at": _now(),
    }
    # Only knockout fixtures carry a phase. Omitting the key for league/group
    # rounds means those crawls never touch the matches.phase column, so they
    # keep working even where the (additive) migration hasn't been applied.
    if phase is not None:
        row["phase"] = phase
    return row


def _match_player_rows(game: dict) -> list[dict]:
    now = _now()
    rows = []
    # The crawler emits players in zerozero lineup order (home XI, away XI,
    # home subs, away subs); persist that order so the UI can mirror the site.
    for i, p in enumerate(game.get("players", [])):
        s = p["stats"]
        m = p.get("minutes", {})
        rows.append({
            "match_id": game["game_id"],
            "player_id": p["id"],
            "team_id": (p.get("team") or {}).get("id"),
            "order_index": i,
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
                scraped_at: str | None, phase: str | None = None) -> None:
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

    match_rows = [_match_row(g, competition_id=competition_id,
                             round_no=round_no, scraped_at=scraped_at,
                             phase=phase)
                  for g in games]

    # Status moves forward only (scheduled -> live -> final): a stale crawl
    # must never revert a finished match back to live/scheduled. Likewise, a
    # live/single-match crawl carries no competition (competition_id is None) --
    # preserve the league already on the row so subscription routing survives.
    rank = {"scheduled": 0, "live": 1, "final": 2, "postponed": 1}
    existing = sb.match_existing([r["id"] for r in match_rows])
    for r in match_rows:
        prev = existing.get(r["id"]) or {}
        cur = prev.get("status")
        if cur and rank.get(cur, 0) > rank.get(r["status"], 0):
            r["status"] = cur
        if r.get("competition_id") is None and prev.get("competition_id"):
            r["competition_id"] = prev["competition_id"]

    sb.upsert("matches", match_rows, "id")

    match_players: list[dict] = []
    for g in games:
        match_players.extend(_match_player_rows(g))
    sb.upsert("match_players", match_players, "match_id,player_id")


# ----------------------------------------------------------------------------
# crawl_runs lifecycle + orchestration
# ----------------------------------------------------------------------------


def _begin_run(sb: Supabase, *, run_id: int | None, trigger: str, kind: str,
               target: str, github_run_id: str | None,
               source: str = "zerozero") -> int | None:
    patch = {"status": "running", "started_at": _now(), "kind": kind,
             "target": target, "trigger": trigger,
             "github_run_id": github_run_id, "source": source}
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


def _maybe_fetch_reporter(game: dict, *, github_run_id: str | None,
                          delay: float) -> None:
    """After a finished single-match crawl, fetch A Bola reporter ratings too.
    A direct match crawl can't tell live from final (the static page doesn't
    say), so 'finished' is taken as a complete final score. Failures are logged,
    never fatal -- the match sync already succeeded and the reporter fetch
    records its own crawl_runs row."""
    result = game.get("result") or {}
    if result.get("home") is None or result.get("away") is None:
        return  # no final score yet -> nothing for A Bola to have rated
    try:
        import reporter_sync  # lazy: reporter_sync imports sync (circular)
        reporter_sync.run(game["game_id"], run_id=None,
                          github_run_id=github_run_id, delay=delay)
    except Exception as err:  # noqa: BLE001
        print(f"  ! reporter auto-fetch failed: {err}", file=sys.stderr)


def _maybe_fetch_round_reporters(games: list[dict], *, round_no: int | None,
                                 github_run_id: str | None, delay: float) -> None:
    """After a round crawl, batch-fetch A Bola reporter ratings for the finished
    games (one collect_cronicas pass). Non-fatal; records its own reporter run.
    run_round() filters to finished games, so passing the whole round is fine."""
    try:
        import reporter_sync  # lazy: reporter_sync imports sync (circular)
        reporter_sync.run_round(games, round_no=round_no, run_id=None,
                                github_run_id=github_run_id, delay=delay)
    except Exception as err:  # noqa: BLE001
        print(f"  ! reporter round auto-fetch failed: {err}", file=sys.stderr)


def _fetch_missing_reporters(sb: Supabase, competition_id: str, *,
                              github_run_id: str | None, delay: float) -> None:
    """Fetch reporter scores for any finished match in this competition that
    still has unlinked players. Runs after a manual crawl so predraft games
    (and any other gaps) get filled in automatically."""
    try:
        match_ids = sb.unscored_finished_matches(competition_id)
        if not match_ids:
            return
        print(f"  reporter gap-fill: {len(match_ids)} match(es) with missing scores",
              file=sys.stderr)
        import reporter_sync  # lazy: reporter_sync imports sync (circular)
        for mid in match_ids:
            try:
                reporter_sync.run(mid, run_id=None, github_run_id=github_run_id,
                                  delay=delay)
            except Exception as err:  # noqa: BLE001
                print(f"  ! reporter gap-fill failed for {mid}: {err}", file=sys.stderr)
    except Exception as err:  # noqa: BLE001
        print(f"  ! reporter gap-fill scan failed: {err}", file=sys.stderr)


def run(*, jornada: int | None, match_url: str | None, run_id: int | None,
        trigger: str, github_run_id: str | None, delay: float,
        competition_url: str | None = None) -> dict:
    sb = Supabase()
    kind = "match" if match_url else "round"
    target = match_url if match_url else str(jornada if jornada is not None else "current")

    run_id = _begin_run(sb, run_id=run_id, trigger=trigger, kind=kind,
                        target=target, github_run_id=github_run_id)
    try:
        if match_url:
            # A single match may be any competition (not necessarily Liga
            # Portugal), so don't tag it with the Liga edition.
            jobstatus.report(f"Crawling match {match_url}")
            game = crawler.get_match(match_url, delay=delay)
            games = [game]
            round_no = None
            scraped_at = _now()
            competition = None
        else:
            jobstatus.report("Resolving competition")
            competition = crawler.get_competition(
                competition_url or crawler.COMPETITION_URL, delay=delay)
            data = crawler.crawl_round(jornada=jornada, competition=competition,
                                       delay=delay)
            games = data["games"]
            round_no = data["round"]
            scraped_at = data.get("scraped_at")

        jobstatus.report(f"Writing {len(games)} game(s) to the database")
        write_games(sb, games, competition=competition, round_no=round_no,
                    scraped_at=scraped_at)
        _finish_run(sb, run_id, status="success", games_count=len(games))
        jobstatus.done("success",
                       message=f"Synced {len(games)} game(s) (round {round_no}).")
        print(f"Synced {len(games)} game(s). crawl_run #{run_id}",
              file=sys.stderr)

        # Chain the A Bola reporter fetch onto manual (dashboard) crawls, for the
        # finished games. Its own crawl_runs row(s); never fails the stats sync.
        # Only on manual: scheduled crawls run often and ratings appear hours
        # post-match. The live watcher uses write_games directly, so it's exempt.
        if trigger == "manual":
            if match_url:
                _maybe_fetch_reporter(games[0], github_run_id=github_run_id,
                                      delay=delay)
            else:
                _maybe_fetch_round_reporters(games, round_no=round_no,
                                             github_run_id=github_run_id,
                                             delay=delay)
            # Gap-fill: fetch reporter scores for any finished match in this
            # competition that still has unlinked players (e.g. predraft games).
            comp_id = competition["id_edicao"] if competition else None
            if not comp_id and games:
                existing = sb.match_existing([games[0].get("game_id", "")])
                row = next(iter(existing.values()), {})
                comp_id = row.get("competition_id")
            if comp_id:
                _fetch_missing_reporters(sb, comp_id, github_run_id=github_run_id,
                                         delay=delay)
        return {"run_id": run_id, "games": len(games)}
    except Exception as err:  # noqa: BLE001
        _finish_run(sb, run_id, status="error", error=str(err)[:2000])
        jobstatus.done("error", message=str(err))
        print(f"ERROR in crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def run_backfill(*, competition_url: str, run_id: int | None, trigger: str,
                 github_run_id: str | None, delay: float,
                 force: bool = False) -> dict:
    """Crawl & sync every round of a competition that isn't already complete.

    Reads the competition landing page once to discover all rounds, then crawls
    each round that is missing from the DB or still has a non-'final' game,
    skipping rounds whose games are all final (unless ``force``). This is the
    entry point for backfilling a league when a subscriber first asks for it.
    """
    sb = Supabase()
    comp = crawler.get_competition(competition_url, delay=delay)
    comp_id = comp.get("id_edicao")
    label = comp.get("slug") or comp.get("name") or competition_url

    run_id = _begin_run(sb, run_id=run_id, trigger=trigger, kind="backfill",
                        target=str(label), github_run_id=github_run_id)
    try:
        # Persist the competition up front so an attempted league always shows
        # up (with its metadata) even if no rounds yield games this run -- e.g.
        # a future-only schedule, or a format the fixture parser can't read yet.
        # write_games() also upserts it per round, but it bails on empty games.
        if comp_id:
            sb.upsert("competitions", [_competition_row(comp)], "id")

        existing = sb.competition_rounds_status(comp_id) if comp_id else {}
        all_rounds = comp.get("rounds") or []

        def needs(rd: int) -> bool:
            if force:
                return True
            statuses = existing.get(rd)
            if not statuses:
                return True  # never crawled -> capture the schedule
            return any(s != "final" for s in statuses)  # still open -> refresh

        todo = [rd for rd in all_rounds if needs(rd)]
        print(f"Backfill '{label}' (edition {comp_id}): {len(all_rounds)} round(s), "
              f"{len(todo)} to crawl, {len(all_rounds) - len(todo)} already final.",
              file=sys.stderr)

        # Chain A Bola reporter ratings for finished games, just like a manual
        # round crawl. A Bola only covers the Portuguese top flight, so skip the
        # reporter pass for other leagues (it would only log empty runs).
        fetch_reporters = comp.get("slug") == COMPETITION_SLUG
        if not fetch_reporters and todo:
            print(f"  (reporter ratings skipped: A Bola covers "
                  f"'{COMPETITION_SLUG}', not '{comp.get('slug')}')",
                  file=sys.stderr)

        total_games = 0
        crawled_rounds = 0
        for n, rd in enumerate(todo, 1):
            jobstatus.report(f"Backfill {label}: round {rd}",
                             current=n, total=len(todo))
            try:
                data = crawler.crawl_round(jornada=rd, competition=comp,
                                           delay=delay)
            except Exception as err:  # noqa: BLE001 - skip a bad round, keep going
                print(f"  ! round {rd} failed: {err}", file=sys.stderr)
                continue
            games = data["games"]
            write_games(sb, games, competition=comp, round_no=data["round"],
                        scraped_at=data.get("scraped_at"))
            total_games += len(games)
            crawled_rounds += 1
            print(f"  round {rd}: synced {len(games)} game(s).", file=sys.stderr)

            # Only when there's a finished game to rate (skip future rounds).
            if fetch_reporters and any(
                (g.get("result") or {}).get("home") is not None
                and (g.get("result") or {}).get("away") is not None
                for g in games
            ):
                _maybe_fetch_round_reporters(games, round_no=data["round"],
                                             github_run_id=github_run_id,
                                             delay=delay)

        # Knockout/cup phases (Oitavos, Quartos, Final, ...). They aren't
        # numbered jornadas; map each to a round number continuing after the
        # group stage and store its readable name in matches.phase. Empty for a
        # plain league, so this whole block is a no-op there.
        group_max = max(all_rounds) if all_rounds else 0
        knockouts = [p for p in (comp.get("phases") or [])
                     if p.get("fase") and p["fase"] != comp.get("fase")]
        for idx, ph in enumerate(knockouts, 1):
            rd = group_max + idx
            if not needs(rd):
                continue
            jobstatus.report(f"Backfill {label}: {ph['name']}")
            try:
                data = crawler.crawl_phase(comp, ph["fase"], round_no=rd,
                                           phase_name=ph["name"], delay=delay)
            except Exception as err:  # noqa: BLE001 - skip a bad phase, keep going
                print(f"  ! phase {ph['name']} failed: {err}", file=sys.stderr)
                continue
            games = data["games"]
            write_games(sb, games, competition=comp, round_no=rd,
                        scraped_at=data.get("scraped_at"), phase=ph["name"])
            total_games += len(games)
            crawled_rounds += 1
            print(f"  {ph['name']} (round {rd}): synced {len(games)} game(s).",
                  file=sys.stderr)

            if fetch_reporters and any(
                (g.get("result") or {}).get("home") is not None
                and (g.get("result") or {}).get("away") is not None
                for g in games
            ):
                _maybe_fetch_round_reporters(games, round_no=rd,
                                             github_run_id=github_run_id,
                                             delay=delay)

        _finish_run(sb, run_id, status="success", games_count=total_games)
        jobstatus.done("success", message=f"Backfilled {crawled_rounds} round(s), "
                       f"{total_games} game(s).")
        print(f"Backfill done: {total_games} game(s) across {crawled_rounds} "
              f"round(s). crawl_run #{run_id}", file=sys.stderr)
        return {"run_id": run_id, "rounds": crawled_rounds, "games": total_games}
    except Exception as err:  # noqa: BLE001
        _finish_run(sb, run_id, status="error", error=str(err)[:2000])
        jobstatus.done("error", message=str(err))
        print(f"ERROR in backfill crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def _env(*names: str) -> str | None:
    """First non-empty environment variable among names."""
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


def _flag(v: str | None) -> bool:
    """Truthy parse for env-var flags ('1'/'true'/'yes'/'on')."""
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _competition_url(arg: str | None) -> str | None:
    """Resolve a competition CLI/env value to a landing-page URL. Accepts a
    full URL or a bare zerozero slug (e.g. 'liga-portuguesa')."""
    if not arg:
        return None
    if arg.startswith("http"):
        return arg
    return f"{crawler.BASE}/competicao/{arg}"


def main() -> int:
    _load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--jornada", type=int,
                      help="Round to crawl & sync (defaults to current round).")
    mode.add_argument("--match", help="Single match URL to crawl & sync.")
    ap.add_argument("--competition",
                    help="Competition to crawl: a zerozero slug (e.g. "
                         "'liga-portuguesa') or a full landing-page URL. "
                         "Defaults to Liga Portugal.")
    ap.add_argument("--backfill", action="store_true",
                    help="Crawl every round of --competition that isn't already "
                         "complete (missing or with non-final games).")
    ap.add_argument("--force", action="store_true",
                    help="With --backfill, re-crawl every round even if final.")
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

    competition_url = _competition_url(args.competition or _env("IN_COMPETITION"))
    backfill = args.backfill or _flag(_env("IN_BACKFILL"))
    force = args.force or _flag(_env("IN_FORCE"))

    if backfill:
        run_backfill(competition_url=competition_url or crawler.COMPETITION_URL,
                     run_id=run_id, trigger=trigger,
                     github_run_id=args.github_run_id, delay=args.delay,
                     force=force)
        return 0

    run(jornada=jornada, match_url=match_url, run_id=run_id,
        trigger=trigger, github_run_id=args.github_run_id, delay=args.delay,
        competition_url=competition_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
