#!/usr/bin/env python3
"""
Transfermarkt squad scraper — detailed player positions WITHOUT per-player pages.

zerozero.pt poisons bot-flagged traffic, and the worst trigger was the roster
"full" sync opening one /jogador/ page *per player* just to read the detailed
"Posição". This module gets that detailed position from Transfermarkt instead:
one league-overview page + one team "kader" (squad) page per club. zerozero is
still used (in roster.py) for the squad's player IDs / groups / shirt numbers —
the IDs that are this project's primary keys.

Pipeline (driven by roster_sync.enrich):
    get_league_teams(code, saison)        -> [{name, slug, verein_id}]   (1 page)
    get_team_squad(slug, verein, saison)  -> [{tm_id, name, shirt, position, ...}]
    match_team / match_player             -> map TM rows back to zerozero ids
    enrich_competition(comp, zz_teams)    -> {enriched, team_tm, stats}

On transfermarkt.pt the position labels are already Portuguese ("Guarda-Redes",
"Defesa Central", "Médio Defensivo", "Ponta de Lança", ...), so they feed straight
into roster.position_code() with no extra mapping.

VERIFIED against real markup (Liga Portugal 25/26, saison_id 2025):
  - league overview: …/startseite/wettbewerb/PO1/plus/?saison_id=2025
      each club row: <td class="hauptlink no-border-links"><a title="SL Benfica" …>
      with a squad link /<kader-slug>/kader/verein/<verein_id>/saison_id/<S>
      (NB: the kader slug differs from the crest/startseite slug — use the kader one)
  - kader detailed: /<slug>/kader/verein/<id>/saison_id/<S>/plus/1
      each player: <td class="… rueckennummer …"><div class=rn_nummer>N</div></td>
                   <td class="posrela"><table class="inline-table">
                     <tr>…<td class="hauptlink"><a href="…/profil/spieler/<id>">Name</a></td></tr>
                     <tr><td> Detailed Position </td></tr></table></td>
                   <td class="zentriert">DD/MM/YYYY (age)</td>
                   <td class="zentriert"><img … title="Nationality" …></td>
      (the captain badge adds a <span> inside the name <a>, so the name is read
       up to </a> and tag-stripped, and the position is anchored on </table>.)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata

import crawler  # reuse: new_session, clean_name, strip_tags
import jobstatus  # best-effort progress heartbeat for the runner tray
import roster   # reuse: position_code (TM's Portuguese labels already match)

TM_BASE = "https://www.transfermarkt.pt"

# Transfermarkt "Wettbewerb" code per competition is configured in the DB
# (competitions.tm_competition_code, set from the backoffice). This map is only a
# built-in seed/fallback for leagues that have no DB value yet, keyed by zerozero
# competition slug. A competition with neither a DB code nor a seed entry is
# skipped for enrichment (the fast zerozero roster still syncs; players just keep
# their position_group).
COMP_CODES: dict[str, str] = {
    "liga-portuguesa": "PO1",
}

# ----------------------------------------------------------------------------
# HTTP — own polite throttle, decoupled from zerozero's (different host, and the
# two sources are crawled sequentially so they shouldn't share a budget).
#   TM_MIN_INTERVAL  seconds between requests (floor; default 4.0)
#   TM_JITTER        extra random 0..N seconds per request (default 2.0)
# ----------------------------------------------------------------------------
MIN_REQUEST_INTERVAL = float(os.environ.get("TM_MIN_INTERVAL", "4.0"))
REQUEST_JITTER = float(os.environ.get("TM_JITTER", "2.0"))
_last_request_ts = 0.0


def _throttle() -> None:
    global _last_request_ts
    interval = max(MIN_REQUEST_INTERVAL, 0.0)
    if interval <= 0 and REQUEST_JITTER <= 0:
        _last_request_ts = time.monotonic()
        return
    target = interval + (random.uniform(0, REQUEST_JITTER) if REQUEST_JITTER > 0 else 0)
    wait = (_last_request_ts + target) - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


def _fetch(session, url: str, *, retries: int = 3) -> str:
    """GET a Transfermarkt URL politely; return decoded HTML. Mirrors
    crawler.fetch's encoding handling (TM serves UTF-8)."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        _throttle()
        try:
            if crawler._CFFI_OK:
                resp = session.get(
                    url, headers={"Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8"},
                    timeout=30)
            else:
                resp = session.get(url, headers=crawler.HEADERS, timeout=30)
            resp.raise_for_status()
            try:
                if not resp.encoding:
                    resp.encoding = (getattr(resp, "apparent_encoding", None)
                                     or "utf-8")
            except Exception:  # noqa: BLE001
                pass
            return resp.text
        except Exception as err:  # noqa: BLE001
            last_err = err
            print(f"  ! TM fetch failed ({attempt}/{retries}) {url}: {err}",
                  file=sys.stderr)
            time.sleep(MIN_REQUEST_INTERVAL * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


# ----------------------------------------------------------------------------
# Competition mapping
# ----------------------------------------------------------------------------


def comp_code_for(comp: dict) -> str | None:
    """The DB-configured code wins; the seed map is a fallback for leagues not
    yet configured in the backoffice."""
    return (comp.get("tm_competition_code")
            or COMP_CODES.get((comp.get("slug") or "").lower()))


def saison_id_from_season(season: str | None) -> str | None:
    """'2025/26' -> '2025' (Transfermarkt keys a season by its start year)."""
    if not season:
        return None
    m = re.search(r"(20\d{2})", season)
    return m.group(1) if m else None


def saison_id_for(comp: dict) -> str | None:
    """The backoffice override wins; else derive from the season label."""
    return comp.get("tm_saison_id") or saison_id_from_season(comp.get("season"))


# ----------------------------------------------------------------------------
# League overview -> teams
# ----------------------------------------------------------------------------

_LEAGUE_ROW_SPLIT = re.compile(r'(?=<td class="hauptlink no-border-links)')
_LEAGUE_NAME_RE = re.compile(r'no-border-links"><a title="([^"]+)"')
_LEAGUE_KADER_RE = re.compile(r'href="/([a-z0-9-]+)/kader/verein/(\d+)/')


def league_url(code: str, saison_id: str) -> str:
    return f"{TM_BASE}/x/startseite/wettbewerb/{code}/plus/?saison_id={saison_id}"


def parse_league_teams(html: str) -> list[dict]:
    teams: list[dict] = []
    seen: set[str] = set()
    for chunk in _LEAGUE_ROW_SPLIT.split(html)[1:]:
        nm = _LEAGUE_NAME_RE.search(chunk)
        kd = _LEAGUE_KADER_RE.search(chunk)
        if not nm or not kd:
            continue
        slug, verein_id = kd.group(1), kd.group(2)
        if verein_id in seen:
            continue
        seen.add(verein_id)
        teams.append({"name": crawler.clean_name(nm.group(1)),
                      "slug": slug, "verein_id": verein_id})
    return teams


def get_league_teams(code: str, saison_id: str, *, session=None) -> list[dict]:
    session = session or crawler.new_session()
    html = _fetch(session, league_url(code, saison_id))
    teams = parse_league_teams(html)
    if not teams:
        print(f"  ! WARNING: no teams parsed from TM league {code}/{saison_id} "
              "(# VERIFY league row regex).", file=sys.stderr)
    return teams


# ----------------------------------------------------------------------------
# Team kader (squad) -> players with detailed position
# ----------------------------------------------------------------------------

_SQUAD_ROW_SPLIT = re.compile(r'(?=<td class="zentriert rueckennummer)')
_SHIRT_RE = re.compile(r"rn_nummer>\s*(\d*)")
_TM_PLAYER_RE = re.compile(r'/([a-z0-9-]+)/profil/spieler/(\d+)">(.*?)</a>', re.S)
_TM_POS_RE = re.compile(r"<td>\s*([^<]+?)\s*</td>\s*</tr>\s*</table>", re.S)
_TM_DOB_RE = re.compile(
    r'</table>\s*</td>\s*<td class="zentriert">\s*'
    r"(\d{2})/(\d{2})/(\d{4})\s*\((\d{1,2})\)")
_TM_NAT_RE = re.compile(r'flagge[^>]*title="([^"]+)"')


def squad_url(slug: str, verein_id: str, saison_id: str) -> str:
    return (f"{TM_BASE}/{slug or 'x'}/kader/verein/{verein_id}"
            f"/saison_id/{saison_id}/plus/1")


def parse_team_squad(html: str) -> list[dict]:
    players: list[dict] = []
    for chunk in _SQUAD_ROW_SPLIT.split(html)[1:]:
        pm = _TM_PLAYER_RE.search(chunk)
        if not pm:
            continue
        slug, tm_id, raw_name = pm.groups()
        name = crawler.clean_name(crawler.strip_tags(raw_name))
        if not name:
            continue
        shirt = _SHIRT_RE.search(chunk)
        pos = _TM_POS_RE.search(chunk)
        dob = _TM_DOB_RE.search(chunk)
        nat = _TM_NAT_RE.search(chunk)
        position = crawler.clean_name(pos.group(1)) if pos else None
        players.append({
            "tm_id": tm_id,
            "slug": slug,
            "name": name,
            "shirt_number": int(shirt.group(1)) if shirt and shirt.group(1) else None,
            "position": position,
            "birth_date": (f"{dob.group(3)}-{dob.group(2)}-{dob.group(1)}"
                           if dob else None),
            "age": int(dob.group(4)) if dob else None,
            "nationality": crawler.clean_name(nat.group(1)) if nat else None,
        })
    return players


def get_team_squad(slug: str, verein_id: str, saison_id: str, *,
                   session=None) -> list[dict]:
    session = session or crawler.new_session()
    html = _fetch(session, squad_url(slug, verein_id, saison_id))
    players = parse_team_squad(html)
    if not players:
        print(f"  ! WARNING: no players parsed for TM team {slug}/{verein_id} "
              "(# VERIFY kader row regex).", file=sys.stderr)
    return players


# ----------------------------------------------------------------------------
# Name normalization + matching (TM rows -> zerozero ids)
# ----------------------------------------------------------------------------

# Club-type tokens dropped before matching team names across the two sources.
_CLUB_TOKENS = {"fc", "sc", "sl", "cd", "cf", "gd", "ac", "sad", "futebol",
                "clube", "praia", "de", "da", "do", "dos", "das"}


def _deaccent(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _tokens(s: str | None) -> set[str]:
    s = _deaccent(s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return {t for t in s.split() if t}


def _team_tokens(s: str | None) -> set[str]:
    toks = _tokens(s) - _CLUB_TOKENS
    return toks or _tokens(s)  # never empty (e.g. "AVS" alone)


def match_team(zz_team: dict, tm_teams: list[dict],
               aliases: dict[str, list[str]] | None = None) -> dict | None:
    """Map a zerozero team to its Transfermarkt club by name (+ aliases).
    Exact normalized match wins; else a unique token subset/superset; else None."""
    names = [zz_team.get("name") or ""]
    names += (aliases or {}).get(zz_team.get("id"), [])
    cand_sets = [_team_tokens(n) for n in names if n]
    tm_sets = [(_team_tokens(t["name"]), t) for t in tm_teams]

    for cs in cand_sets:
        for ts, t in tm_sets:
            if cs and cs == ts:
                return t
    # Subset / superset (e.g. zz "Benfica" ⊂ TM "SL Benfica" -> "benfica").
    subset_hits = []
    for cs in cand_sets:
        for ts, t in tm_sets:
            if cs and ts and (cs <= ts or ts <= cs):
                subset_hits.append(t)
    uniq = {t["verein_id"]: t for t in subset_hits}
    if len(uniq) == 1:
        return next(iter(uniq.values()))
    if len(uniq) > 1:
        print(f"  ? ambiguous TM team match for {zz_team.get('name')!r}: "
              f"{[t['name'] for t in uniq.values()]}", file=sys.stderr)
    return None


def _player_index(tm_players: list[dict]) -> dict:
    by_shirt = {p["shirt_number"]: p for p in tm_players
                if p.get("shirt_number") is not None}
    by_token = [( _tokens(p["name"]), p) for p in tm_players]
    return {"by_shirt": by_shirt, "by_token": by_token}


def match_player(zz_player: dict, idx: dict) -> dict | None:
    """Map a zerozero squad player to a TM squad row: shirt number first (a
    strong key within one official squad), else name token subset/superset with
    birth-year/age as a tiebreaker."""
    shirt = zz_player.get("shirt_number")
    if shirt is not None and shirt in idx["by_shirt"]:
        cand = idx["by_shirt"][shirt]
        # Sanity-guard the shirt match with a shared name token when possible.
        if _tokens(zz_player.get("name")) & _tokens(cand["name"]):
            return cand
        # Shirt numbers are reliable even when the two sources spell the name
        # very differently; accept the shirt match as a fallback.
        return cand

    zt = _tokens(zz_player.get("name"))
    if not zt:
        return None
    hits = [p for ts, p in idx["by_token"] if ts and (zt <= ts or ts <= zt)]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        age = zz_player.get("age")
        narrowed = [p for p in hits if age and p.get("age") == age]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


# ----------------------------------------------------------------------------
# Orchestrator
# ----------------------------------------------------------------------------


def _detail(tm_player: dict) -> dict:
    position = tm_player.get("position")
    return {
        "position": position,
        "position_code": roster.position_code(position),
        "age": tm_player.get("age"),
        "birth_date": tm_player.get("birth_date"),
        "nationality": tm_player.get("nationality"),
    }


def enrich_competition(comp: dict, zz_teams: list[dict], *, session=None,
                       aliases: dict[str, list[str]] | None = None) -> dict:
    """Fill detailed positions for a competition's squads from Transfermarkt.

    `zz_teams` is roster output: [{id, name, slug?, tm_verein_id?, tm_slug?,
    players:[{id, name, shirt_number, age, ...}]}]. Returns:
        enriched : {zz_player_id: {position, position_code, age, birth_date, ...}}
        team_tm  : {zz_team_id: {verein_id, slug}}  newly resolved (to persist)
        stats    : counters for logging
    """
    session = session or crawler.new_session()
    code = comp_code_for(comp)
    saison = saison_id_for(comp)
    if not code or not saison:
        print(f"  ! TM enrichment skipped: no mapping for slug "
              f"{comp.get('slug')!r} / season {comp.get('season')!r}.",
              file=sys.stderr)
        return {"enriched": {}, "team_tm": {},
                "stats": {"teams_matched": 0, "teams_total": len(zz_teams),
                          "players_matched": 0, "players_total": 0,
                          "unmatched_teams": [t.get("name") for t in zz_teams]}}

    enriched: dict[str, dict] = {}
    team_tm: dict[str, dict] = {}
    unmatched_teams: list[str] = []
    players_total = 0
    players_matched = 0
    teams_matched = 0

    league_teams: list[dict] | None = None  # fetched lazily, only if needed
    for n, zz in enumerate(zz_teams, 1):
        jobstatus.report(f"Transfermarkt: {zz.get('name') or zz['id']}",
                         current=n, total=len(zz_teams))
        verein = zz.get("tm_verein_id")
        slug = zz.get("tm_slug")
        if not verein:
            if league_teams is None:
                league_teams = get_league_teams(code, saison, session=session)
            tm = match_team(zz, league_teams, aliases)
            if not tm:
                print(f"  ? no TM team for {zz.get('name') or zz['id']}",
                      file=sys.stderr)
                unmatched_teams.append(zz.get("name") or zz["id"])
                continue
            verein, slug = tm["verein_id"], tm["slug"]
            team_tm[zz["id"]] = {"verein_id": verein, "slug": slug}
        teams_matched += 1

        try:
            squad = get_team_squad(slug, verein, saison, session=session)
        except Exception as err:  # noqa: BLE001
            print(f"  ! TM squad failed for {zz.get('name') or zz['id']}: {err}",
                  file=sys.stderr)
            continue
        idx = _player_index(squad)

        team_players = zz.get("players", [])
        players_total += len(team_players)
        miss = 0
        for p in team_players:
            tm_p = match_player(p, idx)
            if tm_p:
                enriched[p["id"]] = {"id": p["id"], **_detail(tm_p)}
                players_matched += 1
            else:
                miss += 1
        if miss:
            print(f"  ? {miss}/{len(team_players)} players unmatched on TM for "
                  f"{zz.get('name') or zz['id']}", file=sys.stderr)

    stats = {"teams_matched": teams_matched, "teams_total": len(zz_teams),
             "players_matched": players_matched, "players_total": players_total,
             "unmatched_teams": unmatched_teams}
    return {"enriched": enriched, "team_tm": team_tm, "stats": stats}


# ----------------------------------------------------------------------------
# CLI (standalone testing — DB-free)
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition", default="liga-portuguesa",
                    help="zerozero competition slug (mapped via COMP_CODES).")
    ap.add_argument("--code", help="Transfermarkt code directly (overrides map).")
    ap.add_argument("--season", default="2025/26", help="e.g. 2025/26")
    ap.add_argument("--list-teams", action="store_true",
                    help="List the league's TM teams and exit.")
    ap.add_argument("--team", help="A TM verein id to dump the squad for.")
    ap.add_argument("--slug", default="x", help="TM slug for --team.")
    args = ap.parse_args()

    code = args.code or COMP_CODES.get(args.competition.lower())
    saison = saison_id_from_season(args.season)
    if not code or not saison:
        print(f"No TM mapping for {args.competition!r} / {args.season!r}.",
              file=sys.stderr)
        return 2
    session = crawler.new_session()

    if args.team:
        squad = get_team_squad(args.slug, args.team, saison, session=session)
        print(json.dumps(squad, ensure_ascii=False, indent=2))
        return 0

    teams = get_league_teams(code, saison, session=session)
    if args.list_teams or not args.team:
        print(json.dumps(teams, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
