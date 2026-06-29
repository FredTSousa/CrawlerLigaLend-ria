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
import jobstatus  # best-effort progress heartbeat for the runner tray
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
    try:
        resp = sb._rest(
            "GET",
            f"competitions?id=eq.{comp_id}&select=tm_competition_code,tm_saison_id")
    except Exception as err:  # noqa: BLE001
        print(f"  (Transfermarkt config unavailable: {err}; apply "
              "db/migration_transfermarkt.sql to enable enrichment)",
              file=sys.stderr)
        return
    rows = resp.json()
    if rows:
        comp["tm_competition_code"] = rows[0].get("tm_competition_code")
        comp["tm_saison_id"] = rows[0].get("tm_saison_id")


def _persist_team_tm(sb: sync.Supabase, team_tm: dict[str, dict]) -> None:
    """Persist newly-resolved Transfermarkt ids on teams so later runs skip the
    name match (see teams.tm_verein_id / tm_slug). Uses PATCH, not upsert: the
    team rows already exist, and an upsert would propose an insert row missing
    the NOT NULL teams.name (rejected with 23502 even when it would UPDATE)."""
    for tid, v in team_tm.items():
        try:
            sb.update("teams", f"id=eq.{tid}",
                      {"tm_verein_id": v["verein_id"], "tm_slug": v["slug"],
                       "updated_at": sync._now()})
        except Exception as err:  # noqa: BLE001
            print(f"  (could not persist Transfermarkt id for team {tid}: {err})",
                  file=sys.stderr)


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
    inlist = ",".join(ids)
    try:
        names = sb._rest(
            "GET",
            f"teams?id=in.({inlist})&select=id,name,slug,tm_verein_id,tm_slug")
    except Exception:  # noqa: BLE001
        # tm_verein_id / tm_slug exist only after migration_transfermarkt.sql.
        # The fast teams/roster sync must still work before the migration is
        # applied, so fall back to the columns that have always existed.
        names = sb._rest("GET", f"teams?id=in.({inlist})&select=id,name,slug")
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
    # logo_url is written separately (see _persist_team_logos): a team whose
    # squad-page fetch failed has no crest this run, and including it here would
    # null an existing badge (the bulk upsert needs a uniform key set, so it
    # can't be dropped per-row).
    return [{
        "id": t["id"],
        "name": t["name"],
        "slug": t.get("slug"),
        "source_url": t.get("source_url"),
        "last_sync_at": now,
        "updated_at": now,
    } for t in teams if t.get("id")]


def _persist_team_logos(sb: sync.Supabase, teams: list[dict]) -> None:
    """PATCH teams.logo_url only for teams whose crest we captured this run, so a
    failed squad-page fetch (no logo) never wipes an existing badge. Mirrors
    _persist_team_tm: the rows already exist, and PATCH avoids proposing an
    insert missing the NOT NULL teams.name."""
    now = sync._now()
    for t in teams:
        tid, logo = t.get("id"), t.get("logo_url")
        if not tid or not logo:
            continue
        try:
            sb.update("teams", f"id=eq.{tid}",
                      {"logo_url": logo, "updated_at": now})
        except Exception as err:  # noqa: BLE001 - non-fatal, keep syncing
            print(f"  ! logo update failed for team {tid}: {err}", file=sys.stderr)


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


def _player_base_rows(teams: list[dict]) -> list[dict]:
    """One row per distinct player with the columns the roster page provides.
    EVERY row has the same keys — PostgREST rejects a bulk upsert whose objects
    don't all share an identical key set (error PGRST102), so detail columns
    that only some players have are written separately (see _player_detail_rows)."""
    now = sync._now()
    seen: dict[str, dict] = {}
    for t in teams:
        for p in t.get("players", []):
            pid = p.get("id")
            if not pid or pid in seen:
                continue
            seen[pid] = {
                "id": pid,
                "name": p.get("name"),
                "slug": p.get("slug"),
                "position_group": p.get("position_group"),
                "age": p.get("age"),  # roster page gives age inline
                "photo_url": p.get("photo_url"),  # headshot from the team page
                "source_url": p.get("source_url"),
                "last_sync_at": now,
                "updated_at": now,
            }
    return list(seen.values())


def _player_detail_rows(enriched: dict[str, dict]) -> list[dict]:
    """One row per ENRICHED player carrying the Transfermarkt detail. Uniform
    keys across all rows (PGRST102); a None value writes NULL but never drops the
    key. Columns not populated from Transfermarkt (name, club_name, ...) are left
    to _player_base_rows so they're never overwritten here."""
    now = sync._now()
    return [{
        "id": pid,
        "position": d.get("position"),
        "position_code": d.get("position_code"),
        "position_group": d.get("position_group"),
        "age": d.get("age"),
        "birth_date": d.get("birth_date"),
        "nationality": d.get("nationality"),
        "market_value_k": d.get("market_value_k"),  # thousands of euros
        "enriched_at": now,
        "last_sync_at": now,
        "updated_at": now,
    } for pid, d in enriched.items()]


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

    problems: list[tuple[str, dict, str]] = []  # (table, row, error)

    def _save(table: str, rows: list[dict], on_conflict: str) -> None:
        """Upsert that keeps the good rows even if some fail, recording failures
        so they can be reported instead of aborting the whole sync."""
        _, failures = sb.upsert_partial(table, rows, on_conflict)
        problems.extend((table, r, e) for r, e in failures)

    # FK-safe order.
    sb.upsert("competitions", [_competition_patch(comp)], "id")
    _save("teams", _team_rows(teams), "id")
    _persist_team_logos(sb, teams)
    _save("competition_teams", _competition_team_rows(comp_id, teams),
          "competition_id,team_id")

    # Two upserts with uniform key sets each (PostgREST PGRST102): every player
    # gets a base row, then the enriched subset gets its detail columns. The
    # detail rows MUST include name: Postgres validates NOT NULL on an upsert's
    # proposed insert row even when it resolves to an UPDATE, so omitting the
    # NOT NULL players.name fails every row (23502). We carry the roster name so
    # the row is valid on insert and the name is preserved on update.
    base_rows = _player_base_rows(teams)
    base_name = {r["id"]: r["name"] for r in base_rows}
    detail_rows = []
    for r in _player_detail_rows(enriched):
        if r["id"] not in base_name:
            # Enriched but absent from the crawled roster — would insert a player
            # with no roster context. Report it as a non-matching record.
            problems.append(("players(detail)", r,
                             "enriched player not present in the crawled roster"))
            continue
        detail_rows.append({**r, "name": base_name[r["id"]]})
    _save("players", base_rows, "id")
    _save("players", detail_rows, "id")
    _save("roster_memberships", _roster_rows(comp_id, teams),
          "competition_id,team_id,player_id")
    # Patch position_group in roster_memberships from TM-derived data (overrides
    # zerozero section headers, which misclassify wingers as Avançado, etc.).
    if enriched:
        player_team = {p["id"]: t["id"]
                       for t in teams for p in t.get("players", [])
                       if p.get("id") and t.get("id")}
        now = sync._now()
        roster_group_rows = [
            {"competition_id": comp_id, "team_id": player_team[pid],
             "player_id": pid, "position_group": d["position_group"],
             "last_sync_at": now, "updated_at": now}
            for pid, d in enriched.items()
            if d.get("position_group") and pid in player_team
        ]
        if roster_group_rows:
            _save("roster_memberships", roster_group_rows,
                  "competition_id,team_id,player_id")

    if problems:
        print(f"  ! {len(problems)} row(s) failed to save:", file=sys.stderr)
        for table, row, err in problems[:50]:
            ident = row.get("id") or row.get("player_id") or "?"
            name = row.get("name") or ""
            print(f"      [{table}] {ident} {name}: {err}".rstrip(),
                  file=sys.stderr)
        if len(problems) > 50:
            print(f"      … and {len(problems) - 50} more", file=sys.stderr)

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
            "enriched": len(enriched), "failed": len(problems)}


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
            jobstatus.report("Transfermarkt: detailed positions")
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

        jobstatus.report("Writing squads to the database")
        summary = write_squads(sb, data)
        failed = summary.get("failed", 0)
        base_msg = (f"Synced {summary['teams']} team(s), {summary['players']} "
                    f"roster row(s), {summary['enriched']} enriched.")
        # Partial-failure: the good rows are saved, but flag it (red in the tray)
        # so the unsaved records get noticed and the log can be inspected.
        sync._finish_run(sb, run_id, status="success",
                         games_count=summary["players"],
                         error=(f"{failed} row(s) failed to save (see log)"
                                if failed else None))
        if failed:
            jobstatus.done("error",
                           message=f"{base_msg} {failed} row(s) FAILED — see log.")
        else:
            jobstatus.done("success", message=base_msg)
        print(f"{base_msg}"
              + (f" {failed} row(s) failed." if failed else "")
              + f" crawl_run #{run_id}", file=sys.stderr)
        return {"run_id": run_id, **summary}
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        jobstatus.done("error", message=str(err))
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
# Players-only refresh: re-enrich detailed positions from Transfermarkt over the
# rosters ALREADY in the DB, without re-crawling zerozero squad pages.
# ----------------------------------------------------------------------------


def _db_teams_with_rosters(sb: sync.Supabase, comp_id: str) -> list[dict]:
    """Rebuild the teams+players structure from the DB (active roster_memberships
    + player names), so Transfermarkt enrichment can run with no zerozero roster
    crawl. Shape matches roster.crawl_competition_squads output."""
    teams = team_ids_from_matches(sb, comp_id)  # id,name,slug,tm_verein_id,tm_slug
    by_id = {t["id"]: {**t, "players": []} for t in teams if t.get("id")}
    # PostgREST caps a single response (db-max-rows, default 1000). A big
    # competition like the 48-team World Cup has ~1250 active memberships, so an
    # unpaginated read silently drops whole teams' rosters (they fall past row
    # 1000 and arrive with zero players -> never enriched). Page through with an
    # explicit order so limit/offset is deterministic.
    rm: list[dict] = []
    page_size, offset = 1000, 0
    while True:
        page = sb._rest(
            "GET",
            f"roster_memberships?competition_id=eq.{comp_id}&active=is.true"
            "&select=team_id,player_id,shirt_number,age_at_sync,position_group"
            f"&order=team_id,player_id&limit={page_size}&offset={offset}").json()
        rm.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    pids = sorted({r["player_id"] for r in rm if r.get("player_id")})
    pinfo: dict[str, dict] = {}
    for i in range(0, len(pids), 200):
        chunk = pids[i:i + 200]
        for p in sb._rest("GET",
                          f"players?id=in.({','.join(chunk)})&select=id,name,slug").json():
            pinfo[p["id"]] = p
    for r in rm:
        t = by_id.get(r["team_id"])
        if not t:
            continue
        info = pinfo.get(r["player_id"], {})
        t["players"].append({
            "id": r["player_id"], "name": info.get("name"), "slug": info.get("slug"),
            "shirt_number": r.get("shirt_number"), "age": r.get("age_at_sync"),
            "position_group": r.get("position_group"),
        })
    return list(by_id.values())


def run_players(*, competition_url: str, run_id: int | None, trigger: str,
                github_run_id: str | None, delay: float) -> dict:
    """Re-fetch detailed positions from Transfermarkt for a competition's existing
    DB rosters (no zerozero squad crawl). Writes player detail only; team and
    roster membership are left untouched."""
    sb = sync.Supabase()
    comp = crawler.get_competition(competition_url, delay=delay)
    comp_id = comp.get("id_edicao")
    run_id = sync._begin_run(sb, run_id=run_id, trigger=trigger, kind="comp_players",
                             target=str(comp.get("slug") or competition_url),
                             github_run_id=github_run_id)
    try:
        if not comp_id:
            raise RuntimeError("could not resolve competition id_edicao")
        # Season + TM mapping from the DB (avoids a second zerozero request).
        try:
            meta = sb._rest("GET", f"competitions?id=eq.{comp_id}&select="
                            "slug,season,tm_competition_code,tm_saison_id").json()
        except Exception:  # noqa: BLE001
            meta = []
        if meta:
            m = meta[0]
            comp["season"] = comp.get("season") or m.get("season")
            comp.setdefault("slug", m.get("slug"))
            comp["tm_competition_code"] = m.get("tm_competition_code")
            comp["tm_saison_id"] = m.get("tm_saison_id")

        teams = _db_teams_with_rosters(sb, comp_id)
        if not sum(len(t["players"]) for t in teams):
            raise RuntimeError("no rosters in the DB for this competition; run a "
                               "Full sync first to populate the squads")

        jobstatus.report("Transfermarkt: detailed positions")
        aliases = _team_aliases(sb, [t["id"] for t in teams if t.get("id")])
        session = crawler.new_session()
        res = transfermarkt.enrich_competition(comp, teams, session=session,
                                               aliases=aliases)
        if res["team_tm"]:
            _persist_team_tm(sb, res["team_tm"])

        base_name = {p["id"]: p.get("name") for t in teams
                     for p in t["players"] if p.get("id")}
        detail_rows = []
        for r in _player_detail_rows(res["enriched"]):
            nm = base_name.get(r["id"])
            if nm:  # NOT NULL players.name must be present on the upsert row
                detail_rows.append({**r, "name": nm})

        jobstatus.report("Writing player positions to the database")
        saved, failures = sb.upsert_partial("players", detail_rows, "id")
        if failures:
            print(f"  ! {len(failures)} player row(s) failed to save:",
                  file=sys.stderr)
            for row, err in failures[:50]:
                print(f"      {row.get('id')} {row.get('name') or ''}: {err}"
                      .rstrip(), file=sys.stderr)

        # Patch roster_memberships.position_group from TM-derived groups.
        player_team = {p["id"]: t["id"]
                       for t in teams for p in t.get("players", [])
                       if p.get("id") and t.get("id")}
        now = sync._now()
        roster_group_rows = [
            {"competition_id": comp_id, "team_id": player_team[pid],
             "player_id": pid, "position_group": d["position_group"],
             "last_sync_at": now, "updated_at": now}
            for pid, d in res["enriched"].items()
            if d.get("position_group") and pid in player_team
        ]
        if roster_group_rows:
            sb.upsert_partial("roster_memberships", roster_group_rows,
                              "competition_id,team_id,player_id")

        try:
            _rpc(sb, "refresh_competition_counts", {"p_competition_id": comp_id})
        except Exception as err:  # noqa: BLE001
            print(f"  (refresh_competition_counts skipped: {err})", file=sys.stderr)

        s = res["stats"]
        failed = len(failures)
        msg = (f"Transfermarkt: {s['players_matched']}/{s['players_total']} "
               f"matched; saved {saved} position(s).")
        sync._finish_run(sb, run_id, status="success", games_count=saved,
                         error=(f"{failed} row(s) failed (see log)"
                                if failed else None))
        jobstatus.done("error" if failed else "success",
                       message=msg + (f" {failed} FAILED — see log."
                                      if failed else ""))
        print(msg + f" crawl_run #{run_id}", file=sys.stderr)
        return {"run_id": run_id, "matched": s["players_matched"],
                "saved": saved, "failed": failed}
    except Exception as err:  # noqa: BLE001
        sync._finish_run(sb, run_id, status="error", error=str(err)[:2000])
        jobstatus.done("error", message=str(err))
        print(f"ERROR in players crawl_run #{run_id}: {err}", file=sys.stderr)
        raise


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
    ap.add_argument("--players-only", action="store_true",
                    help="Re-fetch detailed positions from Transfermarkt over the "
                         "DB's existing rosters (no zerozero squad crawl).")
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
    players_only = args.players_only or sync._flag(sync._env("IN_PLAYERS_ONLY"))
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

    url = sync._competition_url(competition, sync.Supabase()) or crawler.COMPETITION_URL
    if players_only:
        run_players(competition_url=url, run_id=run_id, trigger=trigger,
                    github_run_id=args.github_run_id, delay=args.delay)
        return 0
    run(competition_url=url, full=full, run_id=run_id, trigger=trigger,
        github_run_id=args.github_run_id, delay=args.delay,
        force_enrich=force_enrich, team_id=team_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
