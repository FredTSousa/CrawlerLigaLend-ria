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
import re
import sys

import abola
import jobstatus  # best-effort progress heartbeat for the runner tray
import providers
import rating_map
import sync

BIG_THREE = ("benfica", "sporting", "porto")


def _is_big_three(home: str, away: str) -> bool:
    blob = abola._norm(home) + "|" + abola._norm(away)
    return any(k in blob for k in BIG_THREE)


def _route_abola_url(url: str, home: str, away: str) -> dict:
    """Map an explicit A Bola page (a UI override for when discovery picked the
    wrong one) to abola.scrape_match kwargs: a combined crónica that carries BOTH
    teams' ratings -> single_url; a single-team 'as notas do X' page -> home_url
    or away_url for whichever side it actually carries. This auto-routing keeps
    the override to one paste-box -- no need to ask which team the page is for."""
    try:
        teams = abola.parse_page(abola.fetch(url))["teams"]
    except Exception:  # noqa: BLE001
        return {"single_url": url}
    has_home = len(abola._pick_team(teams, home)) >= abola.MIN_FEED_RATINGS
    has_away = len(abola._pick_team(teams, away)) >= abola.MIN_FEED_RATINGS
    if has_home and not has_away:
        return {"home_url": url}
    if has_away and not has_home:
        return {"away_url": url}
    return {"single_url": url}


def _load_team_aliases(sb) -> None:
    """Populate abola.TEAM_ALIAS_NAMES from the team_aliases table, so A Bola is
    searched with the names it actually uses (e.g. 'AFS' -> 'Aves SAD'). Best
    effort: a missing table just leaves the built-in aliases in effect."""
    try:
        rows = sb._rest("GET", "team_aliases?select=alias,team:teams(name)").json()
    except Exception:  # noqa: BLE001
        return
    m: dict[str, list[str]] = {}
    for r in rows:
        name = (r.get("team") or {}).get("name")
        if name and r.get("alias"):
            m.setdefault(abola._norm(name), []).append(r["alias"])
    abola.TEAM_ALIAS_NAMES = m


def _fetch_match(sb: sync.Supabase, match_id: str) -> dict | None:
    rows = sb._rest(
        "GET",
        f"matches?id=eq.{match_id}&select=id,played_on,competition_id,"
        "home_team_id,away_team_id,"
        "home:teams!matches_home_team_id_fkey(name),"
        "away:teams!matches_away_team_id_fkey(name)",
    ).json()
    return rows[0] if rows else None


def _competition_cfg(sb: sync.Supabase, competition_id: str | None) -> dict:
    """Reporter config for a competition: which rating source to use and the Goal
    settings. Defaults to A Bola (today's behaviour) when there's no competition
    or no row -- single-match/live crawls that carry no competition stay on A Bola."""
    default = {"rating_source": providers.ABOLA, "goal_fixtures_url": None,
               "rating_mapping": None}
    if not competition_id:
        return default
    try:
        rows = sb._rest(
            "GET",
            f"competitions?id=eq.{competition_id}"
            "&select=rating_source,goal_fixtures_url,rating_mapping",
        ).json()
    except Exception:  # noqa: BLE001 - missing columns (pre-migration) -> A Bola
        return default
    if not rows:
        return default
    r = rows[0]
    return {
        "rating_source": r.get("rating_source") or providers.ABOLA,
        "goal_fixtures_url": r.get("goal_fixtures_url"),
        "rating_mapping": r.get("rating_mapping"),
    }


def _cached_goal_link(sb: sync.Supabase, match_id: str) -> tuple[str | None, str | None]:
    """Previously-discovered Goal URL/id for a match (from matches_reporter_link),
    so a re-crawl reuses it instead of re-searching the fixtures page."""
    try:
        rows = sb._rest(
            "GET",
            f"matches_reporter_link?match_id=eq.{match_id}"
            "&select=goal_match_url,goal_match_id",
        ).json()
    except Exception:  # noqa: BLE001
        return None, None
    if not rows:
        return None, None
    return rows[0].get("goal_match_url"), rows[0].get("goal_match_id")


def _last(name: str) -> str:
    parts = [w for w in re.split(r"\s+", name.strip()) if w]
    return abola._norm(parts[-1]) if parts else abola._norm(name)


def _resolve(rating, zz, aliases, abolaid_map, used, ambiguous):
    """Resolve one A Bola rating to a zerozero player_id, in priority order:
    A Bola id -> learned alias -> exact name -> *unambiguous* surname/substring.
    Returns None (leave for manual linking) rather than guessing when a match is
    ambiguous -- a wrong silent link is worse than a visible gap.
    `zz` is [(player_id, name)]; `used` tracks already-claimed players;
    `ambiguous` is the set of normalized names shared by 2+ players on either
    side (e.g. Gil Vicente's two 'Zé Carlos') -- those are never auto-linked,
    because no rule can tell which is which, so they must be assigned by hand."""
    na = abola._norm(rating["player_name"])
    if not na or na in ambiguous:
        return None
    aid = rating.get("player_id")
    if aid and abolaid_map.get(aid) and abolaid_map[aid] not in used:
        return abolaid_map[aid]
    if aliases.get(na) and aliases[na] not in used:        # learned nickname
        return aliases[na]
    for pid, pn in zz:                                      # exact name
        if pid not in used and abola._norm(pn) == na:
            return pid
    la = _last(rating["player_name"])                      # surname / substring
    cands = []
    for pid, pn in zz:
        if pid in used:
            continue
        npn = abola._norm(pn)
        if (na in npn or npn in na) or _last(pn) == la:
            cands.append(pid)
    return cands[0] if len(cands) == 1 else None


def _link_scores(sb, match_id, team_id, ratings):
    """Attach reporter scores to a team's zerozero match_players. Annotates each
    rating in `ratings` with `zz_player_id` (matched id or None) and returns the
    number matched. Idempotent and manual-safe: clears auto links first, never
    overwrites rows a human edited (reporter_manual=true)."""
    if not team_id:
        for r in ratings:
            r["zz_player_id"] = None
        return 0

    roster = sb._rest(
        "GET",
        f"match_player_details?match_id=eq.{match_id}&team_id=eq.{team_id}"
        "&select=player_id,player_name,reporter_manual",
    ).json()
    zz = [(p["player_id"], p["player_name"]) for p in roster]
    manual = {p["player_id"] for p in roster if p.get("reporter_manual")}

    # Reset prior auto links so dropped/renamed ratings don't leave stale state.
    sb.update("match_players",
              f"match_id=eq.{match_id}&team_id=eq.{team_id}"
              "&reporter_manual=eq.false",
              {"reporter_score": None, "reporter_raw_score": None,
               "reporter_is_mvp": False, "reporter_linked": False})

    aliases = {a["abola_norm"]: a["player_id"] for a in sb._rest(
        "GET",
        f"reporter_aliases?or=(team_id.eq.{team_id},team_id.is.null)"
        "&select=abola_norm,player_id",
    ).json()}
    abolaid_map = {}
    if zz:
        ids = ",".join(p[0] for p in zz)
        abolaid_map = {p["abolaid"]: p["id"] for p in sb._rest(
            "GET",
            f"players?id=in.({ids})&abolaid=not.is.null&select=id,abolaid",
        ).json()}

    # A name is ambiguous when it repeats WITHIN the roster or WITHIN the
    # ratings (two players share it) -> never auto-link, assign by hand.
    def _dups(names):
        c: dict[str, int] = {}
        for n in names:
            c[n] = c.get(n, 0) + 1
        return {n for n, k in c.items() if n and k > 1}

    ambiguous = _dups([abola._norm(pn) for _, pn in zz]) \
        | _dups([abola._norm(r["player_name"]) for r in ratings])

    used, linked = set(), 0
    for r in ratings:
        pid = _resolve(r, zz, aliases, abolaid_map, used, ambiguous)
        r["zz_player_id"] = pid
        if not pid:
            continue
        used.add(pid)
        linked += 1
        if pid in manual:
            continue  # human-set: keep their score, just record the link
        sb.update("match_players",
                  f"match_id=eq.{match_id}&player_id=eq.{pid}",
                  {"reporter_score": r.get("score"),
                   "reporter_raw_score": r.get("raw_score"),
                   "reporter_is_mvp": bool(r["is_mvp"]),
                   "reporter_linked": True})
        if r.get("player_id"):
            sb.update("players", f"id=eq.{pid}", {"abolaid": r["player_id"]})
    return linked


def _store_and_link(sb, match_id, home_team_id, away_team_id, data) -> tuple[int, int]:
    """Persist one match's scraped reporter data + link it to zerozero players.
    Returns (linked, total_ratings). Links first so each rating is annotated
    with its zz_player_id before we store it. The Goal URL/id (when discovered)
    are cached on the row so a re-crawl skips the fixtures search."""
    linked = _link_scores(sb, match_id, home_team_id, data["home_team_ratings"])
    linked += _link_scores(sb, match_id, away_team_id, data["away_team_ratings"])
    row = {
        "match_id": match_id,
        "format_detected": data["format_detected"],
        "urls": data["urls_used"],
        "home_ratings": data["home_team_ratings"],
        "away_ratings": data["away_team_ratings"],
        "fetched_at": sync._now(),
    }
    if data.get("goal_match_url"):
        row["goal_match_url"] = data["goal_match_url"]
        row["goal_match_id"] = data.get("goal_match_id")
    sb.upsert("matches_reporter_link", [row], "match_id")
    total = len(data["home_team_ratings"]) + len(data["away_team_ratings"])
    return linked, total


def _apply_goal_mapping(data: dict, mapping: dict | None) -> None:
    """Fill each Goal rating's 0-10 `score` from its raw value via the
    competition's configured raw-score ranges (rating_map.py). Per-rating and
    independent -- no distribution needed -- so a single match converts on its
    own. Mutates `data` in place before it's stored/linked."""
    for key in ("home_team_ratings", "away_team_ratings"):
        for r in data.get(key) or []:
            r["score"] = rating_map.convert(r.get("raw_score"), mapping)


def run(match_id: str, *, run_id: int | None, github_run_id: str | None,
        delay: float, abola_url: str | None = None) -> dict:
    sync._load_dotenv()
    sb = sync.Supabase()
    _load_team_aliases(sb)
    run_id = sync._begin_run(sb, run_id=run_id, trigger="manual", kind="reporter",
                             target=match_id, github_run_id=github_run_id,
                             source="abola")
    try:
        jobstatus.report(f"Reporter ratings: match {match_id}")
        m = _fetch_match(sb, match_id)
        if not m:
            raise RuntimeError(f"match {match_id} not found")
        home = (m.get("home") or {}).get("name")
        away = (m.get("away") or {}).get("name")
        if not home or not away:
            raise RuntimeError("match has no teams yet (crawl it first)")

        cfg = _competition_cfg(sb, m.get("competition_id"))
        provider = providers.get_provider(
            cfg["rating_source"], goal_fixtures_url=cfg["goal_fixtures_url"])
        if provider.source != "abola":
            sb.update("crawl_runs", f"id=eq.{run_id}", {"source": provider.source})

        match = {"home_team": home, "away_team": away,
                 "game_date": m["played_on"],
                 "is_big_three_match": _is_big_three(home, away)}
        goal_url, goal_id = _cached_goal_link(sb, match_id)
        # A pasted A Bola URL (from the match page's override field) pins the page
        # so discovery is bypassed -- the fix for a wrong auto-matched page that
        # search/feed can no longer reach. Only A Bola pages route this way; a Goal
        # competition ignores it.
        override = (_route_abola_url(abola_url, home, away)
                    if abola_url and provider.source == "abola" else {})
        data = provider.scrape_match(match, delay=delay,
                                     goal_url=goal_url, goal_id=goal_id, **override)
        if provider.source == "goal":
            _apply_goal_mapping(data, cfg["rating_mapping"])
        linked, total = _store_and_link(sb, match_id, m["home_team_id"],
                                        m["away_team_id"], data)
        sync._finish_run(sb, run_id, status="success", games_count=total)
        jobstatus.done("success",
                       message=f"{total} ratings ({linked} linked).")
        print(f"Reporter: {total} ratings ({linked} linked) from "
              f"{len(data['urls_used'])} url(s). crawl_run #{run_id}",
              file=sys.stderr)
        return data
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        jobstatus.done("error", message=str(err))
        print(f"ERROR reporter crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def _round_competition(sb: sync.Supabase, match_ids: list[str]) -> str | None:
    """The competition a round's matches belong to (first non-null), so a batch
    fetch can resolve one rating source for the whole round."""
    if not match_ids:
        return None
    for r in sb.match_existing(match_ids).values():
        if r.get("competition_id"):
            return r["competition_id"]
    return None


def _attach_cached_goal_links(sb: sync.Supabase, matches: list[dict]) -> None:
    """Fill each match dict with any previously-discovered Goal URL/id (keyed by
    game_id) so a re-crawl reuses it instead of re-searching the fixtures page."""
    ids = [m["game_id"] for m in matches if m.get("game_id")]
    if not ids:
        return
    inlist = ",".join(ids)
    rows = sb._rest(
        "GET",
        f"matches_reporter_link?match_id=in.({inlist})"
        "&select=match_id,goal_match_url,goal_match_id",
    ).json()
    cache = {r["match_id"]: r for r in rows}
    for m in matches:
        hit = cache.get(m.get("game_id"))
        if hit:
            m["goal_match_url"] = hit.get("goal_match_url")
            m["goal_match_id"] = hit.get("goal_match_id")


def run_round(games: list[dict], *, round_no: int | None = None,
              run_id: int | None = None, github_run_id: str | None = None,
              delay: float = 1.5) -> list[dict]:
    """Batch reporter fetch for a freshly-crawled round. Takes the crawler's
    game dicts (game_id, home_team/away_team {id,name}, result, date), picks the
    round's competition rating source (A Bola or Goal), scrapes the FINISHED ones
    in a single pass, then stores + links each. For GOAL, a final pass converts
    raw ratings to 0-10 via the competition mapping. One reporter crawl_runs row."""
    sync._load_dotenv()
    sb = sync.Supabase()
    _load_team_aliases(sb)
    run_id = sync._begin_run(
        sb, run_id=run_id, trigger="manual", kind="reporter",
        target=(str(round_no) if round_no is not None else "round"),
        github_run_id=github_run_id, source="abola")
    try:
        matches, meta = [], []
        for g in games:
            res = g.get("result") or {}
            home = (g.get("home_team") or {}).get("name")
            away = (g.get("away_team") or {}).get("name")
            if res.get("home") is None or res.get("away") is None \
                    or not g.get("date") or not home or not away:
                continue  # only finished games with teams + a date
            matches.append({"home_team": home, "away_team": away,
                            "game_date": g["date"], "game_id": g["game_id"],
                            "is_big_three_match": _is_big_three(home, away)})
            meta.append((g["game_id"], (g.get("home_team") or {}).get("id"),
                         (g.get("away_team") or {}).get("id")))

        # The round is one competition -> resolve its config + provider once.
        competition_id = _round_competition(sb, [gid for gid, _, _ in meta])
        cfg = _competition_cfg(sb, competition_id)
        provider = providers.get_provider(
            cfg["rating_source"], goal_fixtures_url=cfg["goal_fixtures_url"])
        if provider.source != "abola":
            sb.update("crawl_runs", f"id=eq.{run_id}", {"source": provider.source})
        if provider.source == "goal":
            _attach_cached_goal_links(sb, matches)

        jobstatus.report(f"Reporter ratings: round {round_no} "
                         f"({len(matches)} game(s))")
        results = provider.scrape_matches(matches, delay=delay) if matches else []
        total = 0
        for (gid, hid, aid), data in zip(meta, results):
            if data.get("error"):
                print(f"  ! reporter {gid}: {data['error']}", file=sys.stderr)
                continue
            if provider.source == "goal":
                _apply_goal_mapping(data, cfg["rating_mapping"])
            _, n = _store_and_link(sb, gid, hid, aid, data)
            total += n
        sync._finish_run(sb, run_id, status="success", games_count=len(matches))
        jobstatus.done("success", message=f"{total} ratings across "
                       f"{len(matches)} finished game(s).")
        print(f"Reporter (round): {total} ratings across {len(matches)} "
              f"finished game(s). crawl_run #{run_id}", file=sys.stderr)
        return results
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        jobstatus.done("error", message=str(err))
        print(f"ERROR reporter round crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--match-id", help="zerozero match id (or env IN_MATCH_ID)")
    ap.add_argument("--run-id", type=int, help="crawl_runs row to update (or IN_RUN_ID)")
    ap.add_argument("--abola-url", help="pin the A Bola page, skip search (or IN_ABOLA_URL)")
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
        delay=args.delay, abola_url=args.abola_url or sync._env("IN_ABOLA_URL"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
