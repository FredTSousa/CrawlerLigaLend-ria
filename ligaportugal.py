#!/usr/bin/env python3
"""
Official Liga Portugal stats (ligaportugal.pt). Job kind 'official'.

The league scores goals / yellows / sub minutes from ligaportugal.pt,
not zerozero, and zerozero can take up to ~48 h to fix its drafts. The site's
frontend reads a public JSON API (no auth, no cookies); this module reads the
same endpoints, stores what the site says in the official_* tables
(db/migration_official_stats.sql) and then applies it to match_players through
apply_official_stats() (db/migration_official_apply.sql): official values win
over zerozero's on every later write, a manual ✎ fix still wins over both.
/official-diff keeps comparing zerozero's raw values with the official ones.

Per finished match (one small request + one squad request):
  v2/match/info/dynamic  -> event feed. occurrenceTypeId: 0 sub in, 1 sub out,
                            2 yellow, 3 straight red (staff get them too),
                            4 goal (penalties included), 5 own goal,
                            6 second-yellow red. entityId 0 = placeholder.
  v1/match/details       -> both squads with shirt numbers, used to map each
                            official player onto the zerozero row with the same
                            shirt in that match (names are too ambiguous:
                            'G. Silva' is two Vitória players). Mappings are
                            cached in players.lp_player_id / teams.lp_team_id.

Assists are not compared: the league keeps taking them from zerozero (the
official site only has them per player, at ~3 MB a request).

The API rate-limits (~50 quick requests -> 429, returned INSIDE a JSON body as
{"code": 400, "message": "...429 Too Many Requests..."}), so requests are paced.

    python ligaportugal.py                       # every competition with lp_competition set
    python ligaportugal.py --competition 218294 --round 8
    python ligaportugal.py --competition 218294 --force   # re-sync already-final matches
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import requests

import jobstatus
import sync

API = "https://www.ligaportugal.pt/api"
MIN_INTERVAL = 1.5          # seconds between requests (rate limit)

# Hourly sweep (--sweep, job kind 'official_sweep'): fetch a finished match soon
# after the whistle, re-check it until the site marks it official, and once more
# a day after kickoff -- so official_stat_history shows whether (and how long
# after the end) official values still change.
SWEEP_RECHECK = timedelta(minutes=50)
SWEEP_SETTLE = timedelta(hours=24)
SWEEP_MAX_AGE = timedelta(days=14)

# Stats tracked in official_stat_history.
HISTORY_STATS = ("goals", "own_goals", "yellow_cards", "red_card",
                 "entered_min", "left_min")

GOAL, OWN_GOAL, YELLOW, RED, SECOND_YELLOW, SUB_IN, SUB_OUT = 4, 5, 2, 3, 6, 0, 1
STAFF_FLAGS = ("isMainCoach", "isAssistantCoach", "isGoalkeeperCoach",
               "isAnalyst", "isDelegate", "isPhysicalTrainer")

# Club-name noise dropped before comparing ligaportugal.pt and zerozero names.
TEAM_STOPWORDS = {"fc", "sc", "cd", "sl", "ac", "cf", "cp", "ud", "gd", "sad",
                  "futebol", "clube", "club", "de", "da", "do", "dos"}


class LPError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# HTTP client
# ----------------------------------------------------------------------------


class LPClient:
    def __init__(self, competition: str, season: str):
        self.competition = competition
        self.season = season
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0",
                                     "Accept": "application/json"})
        self._last = 0.0

    def get(self, path: str, params: dict | None = None, *, retries: int = 4):
        for attempt in range(1, retries + 1):
            wait = MIN_INTERVAL - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                resp = self.session.get(f"{API}/{path}", params=params, timeout=90)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as err:
                err_text = str(err)
            else:
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                # Upstream errors (incl. the 429) arrive as an error object, with
                # either a 200 or a 400 status.
                if isinstance(body, dict) and "code" in body and "message" in body:
                    err_text = f"{body['code']}: {body['message'][:200]}"
                    # Rate limit, or the upstream's cooldown after one.
                    if "429" not in err_text and "circuit breaker" not in err_text:
                        raise LPError(f"{path} -> {err_text}")
                elif resp.ok:
                    return body
                else:
                    err_text = f"HTTP {resp.status_code}"
                    if resp.status_code not in (429, 502, 503, 504):
                        raise LPError(f"{path} -> {err_text}")
            if attempt == retries:
                raise LPError(f"{path} failed after {retries} attempts: {err_text}")
            backoff = 30 * attempt
            print(f"  ! {path}: {err_text}; retrying in {backoff}s", file=sys.stderr)
            time.sleep(backoff)
        raise LPError(f"{path} failed")  # unreachable

    def _q(self, **extra) -> dict:
        return {"competition": self.competition, "season": self.season, **extra}

    def fixtures(self, round_no: int) -> list[dict]:
        return self.get("v1/competition/matches", self._q(round=round_no)) or []

    def dynamic(self, round_no: int, fixture: int) -> dict:
        return self.get("v2/match/info/dynamic", self._q(round=round_no, fixture=fixture))

    def details(self, round_no: int, fixture: int) -> dict:
        return self.get("v1/match/details", self._q(round=round_no, fixture=fixture))


# ----------------------------------------------------------------------------
# Name helpers
# ----------------------------------------------------------------------------


def _tokens(s: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return [t for t in re.sub(r"[^a-z0-9]+", " ", s.lower()).split() if len(t) > 1]


def _tok_eq(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and (a.startswith(b) or b.startswith(a)))


def team_similarity(a: str, b: str) -> float:
    ta = [t for t in _tokens(a) if t not in TEAM_STOPWORDS]
    tb = [t for t in _tokens(b) if t not in TEAM_STOPWORDS]
    if not ta or not tb:
        return 0.0
    hits = sum(1 for x in ta if any(_tok_eq(x, y) for y in tb))
    return hits / max(len(ta), len(tb))


def names_overlap(a: str | None, b: str | None) -> bool:
    tb = _tokens(b)
    return any(len(x) >= 3 and any(_tok_eq(x, y) for y in tb) for x in _tokens(a))


def minute_of(time_text: str | None) -> int | None:
    """"90+5'" -> 90, "45'" -> 45, "após final jogo" -> None."""
    m = re.match(r"\s*(\d+)", time_text or "")
    return int(m.group(1)) if m else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def sweep_due(m: dict, now: datetime) -> bool:
    """Whether the hourly sweep should (re)fetch this finished match."""
    kick = _ts(m.get("kickoff_at"))
    if kick is None or kick < now - SWEEP_MAX_AGE:
        return False
    synced = _ts(m.get("official_synced_at"))
    if synced is None:
        return True                          # first fetch, right after the end
    if m.get("official_final") is not True and synced < now - SWEEP_RECHECK:
        return True                          # not official yet: keep checking
    settle = kick + SWEEP_SETTLE
    return now >= settle and synced < settle  # one more look a day later


def history_rows(old: list[dict], new: list[dict], *, match_id: str,
                 official_final: bool) -> list[dict]:
    """official_stat_history rows for every value that changed between the
    previous fetch (old) and this one (new) of a match."""
    def value(row: dict | None, stat: str):
        if row is None:
            return None if stat.endswith("_min") else 0
        v = row.get(stat)
        return int(v) if isinstance(v, bool) else v

    old_by = {r["lp_player_id"]: r for r in old}
    new_by = {r["lp_player_id"]: r for r in new}
    out = []
    for lp_id in old_by.keys() | new_by.keys():
        o, n = old_by.get(lp_id), new_by.get(lp_id)
        for stat in HISTORY_STATS:
            a, b = value(o, stat), value(n, stat)
            if a != b:
                ref = n or o
                out.append({"match_id": match_id, "lp_player_id": lp_id,
                            "player_id": ref.get("player_id"), "lp_name": ref.get("lp_name"),
                            "stat": stat, "old_value": a, "new_value": b,
                            "official_final": official_final, "observed_at": _now()})
    return out


# ----------------------------------------------------------------------------
# Per-match: official events -> official_match_players rows
# ----------------------------------------------------------------------------


def _squad(details: dict, key: str) -> list[dict]:
    return [p for p in (details.get(key) or [])
            if not any(p.get(f) for f in STAFF_FLAGS) and p.get("externalId")]


def build_rows(dyn: dict, details: dict, *, match: dict, zz_rows: list[dict],
               lp_cache: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """Official per-player rows for one match, plus the lp_player_id -> player_id
    mappings learned from it (by shirt number)."""
    sides = {True: (match["home_team_id"], _squad(details, "homeTeamParticipants")),
             False: (match["away_team_id"], _squad(details, "awayTeamParticipants"))}
    learned: dict[str, str] = {}
    people: dict[str, dict] = {}          # lp id -> {team_id, shirt, name, player_id}

    for home, (team_id, squad) in sides.items():
        team_rows = [r for r in zz_rows if r["team_id"] == team_id]
        by_shirt = {r["shirt_number"]: r for r in team_rows if r["shirt_number"] is not None}
        for p in squad:
            lp_id = str(p["externalId"])
            name = p.get("name")
            pid = None
            row = by_shirt.get(p.get("shirtNumber"))
            # Shirt within the match is the key. The official squad name is
            # sometimes the legal name ('Ignacio Fernández' = Arruabarrena), so
            # a shirt hit is only refused when the name points at someone else.
            if row and (names_overlap(name, row["players"]["name"])
                        or not any(names_overlap(name, r["players"]["name"])
                                   for r in team_rows)):
                pid = row["player_id"]
                learned[lp_id] = pid
            elif lp_id in lp_cache:
                pid = lp_cache[lp_id]
            else:
                cands = [r for r in team_rows if names_overlap(name, r["players"]["name"])]
                if len(cands) == 1:
                    pid = cands[0]["player_id"]
                    learned[lp_id] = pid
            people[lp_id] = {"team_id": team_id, "shirt": p.get("shirtNumber"),
                             "name": name, "player_id": pid, "home": home}

    stats: dict[str, dict] = {}
    for o in dyn.get("occurrences") or []:
        if not o.get("entityId"):
            continue  # placeholder rows
        lp_id = str(o["entityId"])
        t = o.get("occurrenceTypeId")
        if lp_id not in people:
            # Not a squad player: staff cards (full names) and the like.
            continue
        s = stats.setdefault(lp_id, {"goals": 0, "own_goals": 0, "yellow_cards": 0,
                                     "red_card": False, "entered_min": None,
                                     "left_min": None, "lp_name": o.get("playerName")})
        minute = minute_of(o.get("time"))
        if t == GOAL:
            s["goals"] += 1
        elif t == OWN_GOAL:
            s["own_goals"] += 1
        elif t == YELLOW:
            s["yellow_cards"] += 1
        elif t == SECOND_YELLOW:
            # The first yellow is its own type-2 event; zerozero counts a
            # double yellow as yellow_cards = 2, so count this one too.
            s["yellow_cards"] += 1
            s["red_card"] = True
        elif t == RED:
            s["red_card"] = True
        elif t == SUB_IN:
            s["entered_min"] = minute
        elif t == SUB_OUT:
            s["left_min"] = minute

    rows = []
    for lp_id, s in stats.items():
        who = people[lp_id]
        entered, left = s["entered_min"], s["left_min"]
        rows.append({
            "match_id": match["id"], "lp_player_id": lp_id,
            "player_id": who["player_id"], "team_id": who["team_id"],
            "lp_name": s["lp_name"] or who["name"], "shirt_number": who["shirt"],
            "goals": s["goals"], "own_goals": s["own_goals"],
            "yellow_cards": s["yellow_cards"], "red_card": s["red_card"],
            "entered_min": entered, "left_min": left,
            # Same rule as crawler.py (the league's): on at 70'+ or off before 20'.
            "played_under_20m": bool((entered is not None and entered >= 70)
                                     or (left is not None and left < 20)),
            "updated_at": _now(),
        })
    return rows, learned


# ----------------------------------------------------------------------------
# Competition sync
# ----------------------------------------------------------------------------


def _get_all(sb: sync.Supabase, query: str, page: int = 1000) -> list[dict]:
    """GET past PostgREST's 1000-row cap."""
    out: list[dict] = []
    while True:
        batch = sb._rest("GET", f"{query}&limit={page}&offset={len(out)}").json()
        out += batch
        if len(batch) < page:
            return out


def _pair_round(fixtures: list[dict], zz_matches: list[dict], teams: dict[str, dict]
                ) -> list[tuple[dict, dict]]:
    """Pair each official fixture with its zerozero match (same round)."""
    def side_score(lp_team: dict, zz_team_id: str) -> float:
        t = teams.get(zz_team_id) or {}
        if t.get("lp_team_id") and t["lp_team_id"] == str(lp_team.get("external_id")):
            return 2.0
        return team_similarity(lp_team.get("name", ""), t.get("name", ""))

    scored = []
    for f in fixtures:
        for m in zz_matches:
            h, a = side_score(f["homeTeam"], m["home_team_id"]), side_score(f["awayTeam"], m["away_team_id"])
            if h >= 0.5 and a >= 0.5:
                scored.append((h + a, f, m))
    scored.sort(key=lambda x: -x[0])
    pairs, used_f, used_m = [], set(), set()
    for _, f, m in scored:
        if f["fixtureNumber"] in used_f or m["id"] in used_m:
            continue
        used_f.add(f["fixtureNumber"])
        used_m.add(m["id"])
        pairs.append((f, m))
    return pairs


def sync_competition(sb: sync.Supabase, comp: dict, *, round_no: int | None,
                     force: bool, sweep: bool = False) -> int:
    lp = LPClient(comp["lp_competition"], comp["lp_season"])
    comp_id = comp["id"]
    # The league only counts from this round on; earlier rounds are skipped.
    from_round = comp.get("official_from_round") or 1

    matches = sb._rest("GET", (
        f"matches?competition_id=eq.{comp_id}&select=id,round,status,home_team_id,"
        "away_team_id,kickoff_at,official_final,official_synced_at,"
        "official_first_synced_at,lp_round,lp_fixture")).json()
    team_ids = {m[k] for m in matches for k in ("home_team_id", "away_team_id") if m[k]}
    teams = {t["id"]: t for t in sb._rest(
        "GET", f"teams?id=in.({','.join(team_ids)})&select=id,name,lp_team_id").json()} if team_ids else {}
    lp_cache = {r["lp_player_id"]: r["id"] for r in _get_all(
        sb, "players?lp_player_id=not.is.null&select=id,lp_player_id&order=id")}

    now = datetime.now(timezone.utc)

    def todo(m: dict) -> bool:
        if m["status"] != "final":
            return False
        if sweep:
            return sweep_due(m, now)
        return force or m.get("official_final") is not True

    # Newest first: recent zerozero crawls carry shirt numbers (older ones may
    # not), so the players learned there map the older rounds by id.
    rounds = sorted({m["round"] for m in matches
                     if m["round"] is not None and m["round"] >= from_round and todo(m)
                     and (round_no is None or m["round"] == round_no)}, reverse=True)
    synced = 0
    for i, r in enumerate(rounds, 1):
        jobstatus.report(f"Official stats: round {r}", current=i, total=len(rounds))
        zz_round = [m for m in matches if m["round"] == r]
        pairs = _pair_round(lp.fixtures(r), zz_round, teams)
        if len(pairs) < len(zz_round):
            unpaired = {m["id"] for m in zz_round} - {m["id"] for _, m in pairs}
            print(f"  ! round {r}: no official fixture for match(es) {sorted(unpaired)}",
                  file=sys.stderr)

        # Remember confidently paired team ids so later rounds pair directly.
        team_patch = []
        for f, m in pairs:
            for side, tid in (("homeTeam", m["home_team_id"]), ("awayTeam", m["away_team_id"])):
                ext = str(f[side].get("external_id") or "")
                t = teams.get(tid)
                if ext and t and not t.get("lp_team_id"):
                    t["lp_team_id"] = ext
                    team_patch.append({"id": tid, "name": t["name"], "lp_team_id": ext})
        sb.upsert("teams", team_patch, "id")

        work = [(f, m) for f, m in pairs if todo(m)]
        if not work:
            continue
        zz_rows = sb._rest("GET", (
            f"match_players?match_id=in.({','.join(m['id'] for _, m in work)})"
            "&select=match_id,player_id,team_id,shirt_number,players(name)")).json()

        for f, m in work:
            fx = f["fixtureNumber"]
            dyn = lp.dynamic(r, fx)
            if not any(o.get("entityId") for o in dyn.get("occurrences") or []):
                print(f"  - round {r} fixture {fx}: no official events yet", file=sys.stderr)
                continue
            details = lp.details(r, fx)
            rows, learned = build_rows(
                dyn, details, match=m,
                zz_rows=[z for z in zz_rows if z["match_id"] == m["id"]],
                lp_cache=lp_cache)

            final = bool(dyn.get("hasOfficialResult"))
            old = sb._rest("GET", f"official_match_players?match_id=eq.{m['id']}&select=*").json()
            # The first fetch is the baseline; later ones record what changed.
            changes = (history_rows(old, rows, match_id=m["id"], official_final=final)
                       if m.get("official_first_synced_at") else [])

            sb.delete("official_match_players", f"match_id=eq.{m['id']}")
            sb.insert("official_match_players", rows)
            sb.insert("official_stat_history", changes)
            patch = {"lp_round": r, "lp_fixture": fx, "official_synced_at": _now(),
                     "official_final": final}
            if not m.get("official_first_synced_at"):
                patch["official_first_synced_at"] = patch["official_synced_at"]
            sb.update("matches", f"id=eq.{m['id']}", patch)
            m.update(patch)
            applied = sb.rpc("apply_official_stats", {"p_match_id": m["id"]}) or 0

            new = {k: v for k, v in learned.items() if lp_cache.get(k) != v}
            for lp_id, pid in new.items():
                sb.update("players", f"id=eq.{pid}", {"lp_player_id": lp_id})
            lp_cache.update(new)
            unmapped = sum(1 for row in rows if not row["player_id"])
            print(f"  round {r} fixture {fx} -> match {m['id']}: {len(rows)} player(s)"
                  + (f", {unmapped} unmapped" if unmapped else "")
                  + (f", {len(changes)} value(s) changed since last fetch" if changes else "")
                  + (f", {applied} lineup row(s) updated" if applied else ""),
                  file=sys.stderr)
            synced += 1

    remap_unmapped(sb, comp_id, lp_cache)
    return synced


def remap_unmapped(sb: sync.Supabase, comp_id: str, lp_cache: dict[str, str]) -> None:
    """Attach official rows saved without a zerozero player once that player's
    id has been learned from another match (no refetch needed)."""
    rows = _get_all(sb, (
        "official_match_players?player_id=is.null&select=match_id,lp_player_id,"
        f"matches!inner(competition_id)&matches.competition_id=eq.{comp_id}&order=match_id"))
    fixed = 0
    touched: set[str] = set()
    for r in rows:
        pid = lp_cache.get(r["lp_player_id"])
        if pid:
            sb.update("official_match_players",
                      f"match_id=eq.{r['match_id']}&lp_player_id=eq.{r['lp_player_id']}",
                      {"player_id": pid})
            fixed += 1
            touched.add(r["match_id"])
    for match_id in sorted(touched):
        sb.rpc("apply_official_stats", {"p_match_id": match_id})
    if rows:
        print(f"  remapped {fixed} of {len(rows)} unmapped official row(s)", file=sys.stderr)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def main() -> int:
    sync._load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--competition", help="zerozero competition id (default: every "
                                          "competition with lp_competition set)")
    ap.add_argument("--round", type=int, help="only this round")
    ap.add_argument("--from-round", type=int,
                    help="override competitions.official_from_round")
    ap.add_argument("--force", action="store_true",
                    help="re-sync matches already marked officially final")
    ap.add_argument("--sweep", action="store_true",
                    help="hourly cron: fetch the finished matches that are due "
                         "(after the end, until official, and a day after kickoff)")
    ap.add_argument("--run-id", type=int)
    ap.add_argument("--trigger", default="manual", choices=["manual", "schedule"])
    ap.add_argument("--github-run-id", default=os.environ.get("GITHUB_RUN_ID"))
    args = ap.parse_args()

    competition = args.competition or sync._env("IN_COMPETITION")
    round_no = args.round
    if round_no is None and sync._env("IN_JORNADA"):
        round_no = int(sync._env("IN_JORNADA"))
    force = args.force or sync._flag(sync._env("IN_FORCE"))
    run_id = args.run_id
    if run_id is None and sync._env("IN_RUN_ID"):
        run_id = int(sync._env("IN_RUN_ID"))

    sb = sync.Supabase()
    q = "competitions?lp_competition=not.is.null&select=*"
    if competition:
        q += f"&id=eq.{competition}"
    comps = sb._rest("GET", q).json()

    sweep = args.sweep or sync._flag(sync._env("IN_OFFICIAL_SWEEP"))

    def begin() -> int | None:
        return sync._begin_run(
            sb, run_id=run_id, trigger="schedule" if sweep else args.trigger,
            kind="official_sweep" if sweep else "official",
            target=competition or "all", github_run_id=args.github_run_id,
            source="ligaportugal")

    # The hourly sweep only logs a run when it had something to do.
    run_id = None if sweep else begin()
    try:
        if not comps:
            raise LPError(f"no competition with an official source ({competition or 'any'}); "
                          "set competitions.lp_competition / lp_season")
        synced = 0
        for comp in comps:
            if args.from_round:
                comp["official_from_round"] = args.from_round
            print(f"{comp['name']}: official stats", file=sys.stderr)
            synced += sync_competition(sb, comp, round_no=round_no, force=force,
                                       sweep=sweep)
    except Exception as err:
        sync._finish_run(sb, run_id if run_id is not None else begin(),
                         status="error", error=str(err)[:1000])
        jobstatus.done("error", message=str(err))
        raise
    if run_id is None and synced:
        run_id = begin()
    sync._finish_run(sb, run_id, status="success", games_count=synced)
    jobstatus.done("success", message=f"{synced} match(es) compared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
