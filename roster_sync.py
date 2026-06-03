#!/usr/bin/env python3
"""
Sync competition squads (teams + players) into Supabase, with crawl-run logging.

The squad-shaped twin of sync.py. Crawls a competition's teams & rosters with
roster.py, then UPSERTs into Supabase via PostgREST in FK-safe order and records
the outcome in crawl_runs (so the backoffice shows status + history).

Writes, in order:
    competitions (enrich)  ->  teams  ->  competition_teams (membership)
    ->  players (thin/enriched)  ->  roster_memberships (squad)
Then RECONCILES: any membership/roster row for this competition that was NOT
seen this run is soft-deleted (active=false, left_at=now) — history preserved.
Finally stamps competitions.last_sync_at + counts.

Environment (same as sync.py):
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

Usage:
    python roster_sync.py --competition liga-portuguesa              # FAST sync
    python roster_sync.py --competition liga-portuguesa --full       # FULL sync
    python roster_sync.py --competition liga-portuguesa --full --force-enrich
    python roster_sync.py --competition liga-portuguesa --team 32    # one team's roster
    python roster_sync.py --player 445566                            # enrich one player
"""

from __future__ import annotations

import argparse
import os
import sys

import crawler
import roster
import sync  # reuse Supabase, _begin_run, _finish_run, _now, _load_dotenv, _env, _flag
import transfermarkt  # detailed positions WITHOUT per-player zerozero pages


# ----------------------------------------------------------------------------
# Supabase reads this module needs (built on sync.Supabase._rest)
# ----------------------------------------------------------------------------


def _team_aliases(sb: sync.Supabase, team_ids: list[str]) -> dict[str, list[str]]:
    """Map team_id -> [alias, ...] from team_aliases, so Transfermarkt team
    matching can fall back to the alternate club names the backoffice records."""
    out: dict[str, list[str]] = {}
    for i in range(0, len(team_ids), 200):  # chunk to keep URLs short
        chunk = team_ids[i:i + 200]
        if not chunk:
            continue
        resp = sb._rest(
            "GET",
            f"team_aliases?team_id=in.({','.join(chunk)})&select=team_id,alias")
        for r in resp.json():
            out.setdefault(r["team_id"], []).append(r["alias"])
    return out


def _load_tm_config(sb: sync.Supabase, comp: dict, comp_id: str) -> None:
    """Merge the backoffice-set Transfermarkt mapping (competitions.
    tm_competition_code / tm_saison_id) onto the scraped comp dict, in place."""
    resp = sb._rest(
        "GET",
        f"competitions?id=eq.{comp_id}&select=tm_competition_code,tm_saison_id")
    rows = resp.json()
    if rows:
        comp["tm_competition_code"] = rows[0].get("tm_competition_code")
        comp["tm_saison_id"] = rows[0].get("tm_saison_id")


def _persist_team_tm(sb: sync.Supabase, team_tm: dict[str, dict]) -> None:
    """Persist newly-resolved Transfermarkt ids on teams so later runs skip the
    name match (see teams.tm_verein_id / tm_slug)."""
    rows = [{"id": tid, "tm_verein_id": v["verein_id"], "tm_slug": v["slug"],
             "updated_at": sync._now()} for tid, v in team_tm.items()]
    sb.upsert("teams", rows, "id")


def _rpc(sb: sync.Supabase, fn: str, args: dict) -> None:
    sb._rest("POST", f"rpc/{fn}", prefer="return=minimal", json=args)


def team_ids_from_matches(sb: sync.Supabase, competition_id: str) -> list[dict]:
    """Authoritative team set for a competition: the distinct home/away teams of
    its crawled fixtures, with names from the teams table. This is the reliable
    discovery source (the competition landing page only lists one round + a
    sidebar of unrelated clubs). Returns [{id, name}]."""
    resp = sb._rest(
        "GET",
        f"matches?competition_id=eq.{competition_id}"
        "&select=home_team_id,away_team_id")
    ids: set[str] = set()
    for m in resp.json():
        for k in ("home_team_id", "away_team_id"):
            if m.get(k):
                ids.add(m[k])
    if not ids:
        return []
    names = sb._rest(
        "GET",
        f"teams?id=in.({','.join(ids)})&select=id,name,slug,tm_verein_id,tm_slug")
    by_id = {r["id"]: r for r in names.json()}
    return [{"id": tid, "name": by_id.get(tid, {}).get("name"),
             "slug": by_id.get(tid, {}).get("slug"),
             "tm_verein_id": by_id.get(tid, {}).get("tm_verein_id"),
             "tm_slug": by_id.get(tid, {}).get("tm_slug")}
            for tid in sorted(ids)]


# ----------------------------------------------------------------------------
# Row builders
# ----------------------------------------------------------------------------


def _competition_patch(comp: dict) -> dict:
    return {k: v for k, v in {
        "id": comp.get("id_edicao"),
        "name": comp.get("name"),
        "full_name": comp.get("full_name") or comp.get("name"),
        "slug": comp.get("slug"),
        "fase": comp.get("fase"),
        "season": comp.get("season"),
        "epoca_id": comp.get("epoca_id"),
        "source_url": comp.get("source_url") or comp.get("url"),
        "last_sync_at": sync._now(),
        "updated_at": sync._now(),
    }.items() if v is not None or k in ("last_sync_at", "updated_at")}


def _team_rows(teams: list[dict]) -> list[dict]:
    now = sync._now()
    return [{
        "id": t["id"],
        "name": t["name"],
        "slug": t.get("slug"),
        "logo_url": t.get("logo_url"),
        "source_url": t.get("source_url"),
        "last_sync_at": now,
        "updated_at": now,
    } for t in teams if t.get("id")]


def _competition_team_rows(comp_id: str, teams: list[dict]) -> list[dict]:
    now = sync._now()
    return [{
        "competition_id": comp_id,
        "team_id": t["id"],
        "source_url": t.get("source_url"),
        "active": True,
        "last_sync_at": now,
        "updated_at": now,
    } for t in teams if t.get("id")]


def _player_rows_thin(teams: list[dict], enriched: dict[str, dict]) -> list[dict]:
    """One row per distinct player. Enriched players carry detail; others are
    thin (id/name/slug/group) so the FK exists for roster_memberships."""
    now = sync._now()
    seen: dict[str, dict] = {}
    for t in teams:
        for p in t.get("players", []):
            if not p.get("id") or p["id"] in seen:
                continue
            row = {"id": p["id"], "name": p["name"], "slug": p.get("slug"),
                   "position_group": p.get("position_group"),
                   "age": p.get("age"),  # roster page gives age inline
                   "source_url": p.get("source_url"), "updated_at": now}
            d = enriched.get(p["id"])
            if d:
                row.update({k: v for k, v in {
                    "name": d.get("name") or p["name"],
                    "position": d.get("position"),
                    "position_code": d.get("position_code"),
                    "age": d.get("age"),
                    "birth_date": d.get("birth_date"),
                    "club_name": d.get("club_name"),
                    "nationality": d.get("nationality"),
                    "enriched_at": now,
                    "last_sync_at": now,
                }.items() if v is not None})
            else:
                row["last_sync_at"] = now
            seen[p["id"]] = row
    return list(seen.values())


def _roster_rows(comp_id: str, teams: list[dict]) -> list[dict]:
    now = sync._now()
    rows = []
    for t in teams:
        for p in t.get("players", []):
            if not p.get("id"):
                continue
            rows.append({
                "competition_id": comp_id,
                "team_id": t["id"],
                "player_id": p["id"],
                "position_group": p.get("position_group"),
                "shirt_number": p.get("shirt_number"),
                "age_at_sync": p.get("age"),
                "active": True,
                "left_at": None,
                "last_sync_at": now,
                "updated_at": now,
            })
    return rows


# ----------------------------------------------------------------------------
# Writing (+ reconcile)
# ----------------------------------------------------------------------------


def write_squads(sb: sync.Supabase, data: dict) -> dict:
    """Upsert competition/teams/membership/players/roster, then reconcile
    (soft-delete) anything no longer present, and stamp counts."""
    comp = data["competition"]
    comp_id = comp.get("id_edicao")
    if not comp_id:
        raise RuntimeError("competition has no id_edicao; cannot write squads")
    teams = data["teams"]
    enriched = data.get("enriched_players", {})

    # FK-safe order.
    sb.upsert("competitions", [_competition_patch(comp)], "id")
    sb.upsert("teams", _team_rows(teams), "id")
    sb.upsert("competition_teams", _competition_team_rows(comp_id, teams),
              "competition_id,team_id")
    sb.upsert("players", _player_rows_thin(teams, enriched), "id")
    sb.upsert("roster_memberships", _roster_rows(comp_id, teams),
              "competition_id,team_id,player_id")

    # Reconcile membership: teams in the DB for this comp but not seen -> inactive.
    seen_team_ids = [t["id"] for t in teams if t.get("id")]
    _deactivate_missing(sb, "competition_teams",
                        f"competition_id=eq.{comp_id}", "team_id", seen_team_ids)

    # Reconcile roster PER TEAM (so a transfer never deactivates the new club's
    # row). Only touch teams we actually crawled this run.
    for t in teams:
        if not t.get("id"):
            continue
        seen_pids = [p["id"] for p in t.get("players", []) if p.get("id")]
        _deactivate_missing(
            sb, "roster_memberships",
            f"competition_id=eq.{comp_id}&team_id=eq.{t['id']}",
            "player_id", seen_pids, with_left_at=True)

    _rpc(sb, "refresh_competition_counts", {"p_competition_id": comp_id})
    # Emit CompetitionSyncCompleted (entity_outbox) so subscribers can do one
    # bulk pull instead of reacting to every PlayerUpdated. Non-fatal: the export
    # migration may not be applied yet, so a missing RPC must not fail the sync.
    try:
        _rpc(sb, "emit_comp_sync", {"p_competition_id": comp_id})
    except Exception as err:  # noqa: BLE001
        print(f"  (emit_comp_sync skipped: {err})", file=sys.stderr)
    return {"competition_id": comp_id, "teams": len(teams),
            "players": sum(len(t.get("players", [])) for t in teams),
            "enriched": len(enriched)}


def _deactivate_missing(sb: sync.Supabase, table: str, scope: str, key: str,
                        seen: list[str], *, with_left_at: bool = False) -> None:
    """Set active=false for rows in `scope` whose `key` is not in `seen`.
    PostgREST `not.in` filter; skips when nothing was seen (avoids nuking a team
    whose roster failed to parse — better stale than wrongly emptied)."""
    if not seen:
        return  # parse likely failed for this team; don't deactivate everyone
    inlist = ",".join(seen)
    patch = {"active": False, "updated_at": sync._now()}
    if with_left_at:
        patch["left_at"] = sync._now()
    # Only flip rows currently active (avoids re-stamping left_at on old rows).
    sb.update(table, f"{scope}&{key}=not.in.({inlist})&active=is.true", patch)


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


def run(*, competition_url: str, full: bool, run_id: int | None, trigger: str,
        github_run_id: str | None, delay: float, force_enrich: bool = False,
        team_id: str | None = None) -> dict:
    sb = sync.Supabase()
    kind = "comp_full" if full else ("roster" if team_id else "teams")
    comp = crawler.get_competition(competition_url, delay=delay)
    target = f"{comp.get('slug') or competition_url}" + (f"/team:{team_id}" if team_id else "")

    run_id = sync._begin_run(sb, run_id=run_id, trigger=trigger, kind=kind,
                             target=str(target), github_run_id=github_run_id)
    try:
        comp = roster.augment_competition(comp, delay=delay)
        comp_id = comp.get("id_edicao")
        if not comp_id:
            raise RuntimeError("could not resolve competition id_edicao")

        # Pull the backoffice-configured Transfermarkt mapping onto the (scraped)
        # comp dict so enrichment knows which TM competition/season to read.
        if full:
            _load_tm_config(sb, comp, comp_id)

        # Team set: a single team, or all teams of the competition (from matches).
        if team_id:
            db_teams = team_ids_from_matches(sb, comp_id)
            team = next((t for t in db_teams if t["id"] == team_id),
                        {"id": team_id, "name": None})
            teams = [team]
        else:
            teams = team_ids_from_matches(sb, comp_id)
            if not teams:
                raise RuntimeError(
                    f"no teams found for competition {comp_id}; crawl its "
                    "fixtures first (sync.py --backfill) so the team set exists")

        # Fast roster crawl (always): zerozero team pages give the player ids,
        # groups, shirt numbers and ages — one page per team.
        data = roster.crawl_competition_squads(comp, teams, full=False,
                                               delay=delay)
        if full:
            # Detailed positions come from Transfermarkt (one league page + one
            # squad page per club), NOT from per-player zerozero pages. The whole
            # team is refreshed each run, so `force_enrich` no longer applies.
            aliases = _team_aliases(
                sb, [t["id"] for t in data["teams"] if t.get("id")])
            session = crawler.new_session()
            res = transfermarkt.enrich_competition(
                comp, data["teams"], session=session, aliases=aliases)
            data["enriched_players"] = res["enriched"]
            if res["team_tm"]:
                _persist_team_tm(sb, res["team_tm"])
            s = res["stats"]
            print(f"Transfermarkt: {s['teams_matched']}/{s['teams_total']} teams, "
                  f"{s['players_matched']}/{s['players_total']} players matched"
                  + (f"; unmatched teams: {s['unmatched_teams']}"
                     if s["unmatched_teams"] else ""), file=sys.stderr)

        summary = write_squads(sb, data)
        sync._finish_run(sb, run_id, status="success",
                         games_count=summary["players"])
        print(f"Synced {summary['teams']} team(s), {summary['players']} roster "
              f"row(s), {summary['enriched']} enriched. crawl_run #{run_id}",
              file=sys.stderr)
        return {"run_id": run_id, **summary}
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        print(f"ERROR in squad crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


def run_player(player_id: str, *, delay: float) -> dict:
    """Enrich a single player (no competition context)."""
    sb = sync.Supabase()
    d = roster.get_player_detail(player_id, delay=delay)
    sb.upsert("players", [{k: v for k, v in {
        "id": d["id"], "name": d.get("name"), "position": d.get("position"),
        "position_code": d.get("position_code"), "age": d.get("age"),
        "birth_date": d.get("birth_date"), "club_name": d.get("club_name"),
        "nationality": d.get("nationality"), "source_url": d.get("source_url"),
        "enriched_at": sync._now(), "last_sync_at": sync._now(),
        "updated_at": sync._now(),
    }.items() if v is not None}], "id")
    print(f"Enriched player {player_id}.", file=sys.stderr)
    return d


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main() -> int:
    sync._load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition",
                    help="Competition slug or landing-page URL (default Liga Portugal).")
    ap.add_argument("--full", action="store_true",
                    help="Full sync: also open every player detail page.")
    ap.add_argument("--force-enrich", action="store_true",
                    help="With --full, re-enrich even freshly-updated players.")
    ap.add_argument("--team", help="Refresh only this team id's roster.")
    ap.add_argument("--player", help="Enrich one player id and exit.")
    ap.add_argument("--run-id", type=int)
    ap.add_argument("--trigger", default="manual", choices=["manual", "schedule"])
    ap.add_argument("--github-run-id", default=os.environ.get("GITHUB_RUN_ID"))
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()

    # Env fallbacks (set by the workflow), mirroring sync.py.
    competition = args.competition or sync._env("IN_COMPETITION")
    full = args.full or sync._flag(sync._env("IN_FULL"))
    force_enrich = args.force_enrich or sync._flag(sync._env("IN_FORCE"))
    team_id = args.team or sync._env("IN_TEAM")
    player_id = args.player or sync._env("IN_PLAYER")
    run_id = args.run_id
    if run_id is None:
        rid = sync._env("IN_RUN_ID")
        run_id = int(rid) if rid else None
    trigger = args.trigger
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        trigger = "schedule"

    if player_id:
        run_player(player_id, delay=args.delay)
        return 0

    url = sync._competition_url(competition) or crawler.COMPETITION_URL
    run(competition_url=url, full=full, run_id=run_id, trigger=trigger,
        github_run_id=args.github_run_id, delay=args.delay,
        force_enrich=force_enrich, team_id=team_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
