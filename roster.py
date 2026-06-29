#!/usr/bin/env python3
"""
Roster crawler for zerozero.pt — Teams & Players within a Competition.

The squad-shaped twin of crawler.py, reusing crawler.py's HTTP session, polite
fetch, encoding handling and name cleaning. Stages:

    augment_competition(comp)        -> resolve epoca_id + season label
    get_team_roster(epoca_id, team)  -> squad grouped by position_group (FAST)
    get_player_detail(player)        -> detailed "Posição", age, club (FULL)

Team discovery is NOT scraped from the competition landing page (that page only
shows one round's fixtures plus a sidebar of unrelated clubs). Instead the sync
layer derives the team set from the `matches` already crawled for the
competition — authoritative and complete. See roster_sync.team_ids_from_matches.

VERIFIED against real markup (Liga Portugal 2025/26, época 155):
  - squad groups: <div class="section">Guarda Redes|Defesa|Médio|Avançado</div>
    (the first four sections; later sections are staff/honours and are ignored)
  - each player:  <div class="staff"> … <div class="number">N</div> …
                  <div class="photo" style="background-image: url('…')"></div> …
                  <a href="/jogador/<slug>/<id>?epoca_id=…">Name</a> …
                  <span>NN anos - …</span>
  - team page resolves by id alone: /equipa/_/<id>?epoca_id=<epoca>
  - player position: <span class="card-data__label">Posição</span> … later
                     <span class="card-data__value">Guarda Redes</span>
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import crawler  # reuse: new_session, fetch, clean_name, BASE
import jobstatus  # best-effort progress heartbeat for the runner tray

BASE = crawler.BASE

# ----------------------------------------------------------------------------
# Position normalization — single source of truth for export-stable codes.
# ----------------------------------------------------------------------------

POSITION_CODES: dict[str, str] = {
    "guarda-redes": "GR", "guarda redes": "GR",
    "defesa central": "DC",
    "defesa direito": "DL", "lateral direito": "DL",
    "defesa esquerdo": "DL", "lateral esquerdo": "DL",
    "ala direito": "DL", "ala esquerdo": "DL",
    "médio defensivo": "MC", "medio defensivo": "MC",
    "médio centro": "MC", "medio centro": "MC", "médio": "MC", "medio": "MC",
    "médio ofensivo": "MC", "medio ofensivo": "MC",
    "médio direito": "MC", "medio direito": "MC",
    "médio esquerdo": "MC", "medio esquerdo": "MC",
    "extremo direito": "MA", "extremo esquerdo": "MA",
    "segundo avançado": "AV", "segundo avancado": "AV",
    "ponta de lança": "AV", "ponta de lanca": "AV",
    "avançado": "AV", "avancado": "AV",
}

# Maps position_code -> canonical position_group (derived from TM, overrides zerozero sections).
CODE_TO_GROUP: dict[str, str] = {
    "GR": "Guarda Redes",
    "DC": "Defesa", "DL": "Defesa",
    "MC": "Médio", "MA": "Médio",
    "AV": "Avançado",
}


def group_from_code(code: str | None) -> str | None:
    return CODE_TO_GROUP.get(code) if code else None

# Roster section headers on the team page -> canonical position_group.
GROUP_CANON: list[tuple[re.Pattern, str]] = [
    (re.compile(r"guarda.?redes", re.I),     "Guarda Redes"),
    (re.compile(r"^defesas?$", re.I),         "Defesa"),
    (re.compile(r"^m[ée]dios?$", re.I),       "Médio"),
    (re.compile(r"^avan[çc]ados?$", re.I),    "Avançado"),
]


def position_code(position: str | None) -> str | None:
    """Normalize a detailed Portuguese position to an export-stable code.
    Unknown values return None and are logged so the map can be extended."""
    if not position:
        return None
    key = re.sub(r"\s+", " ", position.strip().lower())
    code = POSITION_CODES.get(key)
    if code is None:
        print(f"  ? unmapped position: {position!r}", file=sys.stderr)
    return code


def canon_group(header_text: str | None) -> str | None:
    if not header_text:
        return None
    h = header_text.strip()
    for pat, canon in GROUP_CANON:
        if pat.search(h):
            return canon
    return None


# ----------------------------------------------------------------------------
# Resolve epoca_id + season for a competition (from its landing page)
# ----------------------------------------------------------------------------

EPOCA_ID_RE = re.compile(r"epoca_id=(\d+)")
SEASON_LABEL_RE = re.compile(r"\b(20\d{2})\s*[/\-]\s*(\d{2,4})\b")


def augment_competition(comp: dict, *, session=None, delay: float = 1.0) -> dict:
    """Add `epoca_id`, `season`, `full_name`, `source_url` to a
    crawler.get_competition() dict. Idempotent."""
    if comp.get("epoca_id") and comp.get("season"):
        return comp
    session = session or crawler.new_session()
    html = crawler.fetch(session, comp["url"], delay=delay)
    epoca = EPOCA_ID_RE.search(html)
    season_m = (SEASON_LABEL_RE.search(comp.get("name") or "")
                or SEASON_LABEL_RE.search(html))
    comp = dict(comp)
    comp["epoca_id"] = comp.get("epoca_id") or (epoca.group(1) if epoca else None)
    if season_m:
        comp["season"] = f"{season_m.group(1)}/{season_m.group(2)[-2:]}"
    comp["full_name"] = comp.get("name")
    comp["source_url"] = comp["url"]
    if not comp["epoca_id"]:
        print("  ! WARNING: could not resolve epoca_id; rosters may resolve to "
              "the wrong season.", file=sys.stderr)
    return comp


# ----------------------------------------------------------------------------
# Stage B — a team's squad, grouped by position_group (FAST sync)
# ----------------------------------------------------------------------------

SECTION_RE = re.compile(r'<div class="section">([^<]+)</div>')
# Split on the player card div, tolerant of extra classes: zerozero marks some
# squad members (e.g. departed/loaned, like a captain who has since left) with
# <div class="staff inactive">. The exact-string split missed those, merging
# them into the previous card and silently dropping them — so players who DID
# appear in matches (e.g. Otamendi) never made it into the roster.
STAFF_SPLIT_RE = re.compile(r'<div class="staff[ "]')
STAFF_NUMBER_RE = re.compile(r'<div class="number">\s*(\d*)\s*</div>')
STAFF_PLAYER_RE = re.compile(r'/jogador/([a-z0-9-]+)/(\d+)[^"]*">\s*([^<]+?)\s*</a>')
STAFF_AGE_RE = re.compile(r'>\s*(\d{1,2})\s*anos')
# The headshot is a CSS background-image on <div class="photo">, not an <img>:
#   <div class="photo" style="background-image: url('https://cdn-img.staticzz.com/
#   img/planteis/new/19/52/14901952_anatoliy_trubin_20250809115631.png')"></div>
STAFF_PHOTO_RE = re.compile(
    r'class="photo"[^>]*background-image:\s*url\(\s*([\'"]?)([^\'")]+)\1\s*\)')
# A prominent player can link WITHOUT the numeric id (/jogador/<slug>?edicao_id=…
# — e.g. Cristiano Ronaldo), the /jogador twin of the big-club crest quirk. Then
# read the id from the photo filename (img/jogadores/.../<id>_<slug>_<ts>.png),
# exactly like the team-crest trick in crawler.py.
STAFF_PLAYER_NOID_RE = re.compile(r'/jogador/([a-z0-9-]+)[^"]*">\s*([^<]+?)\s*</a>')
STAFF_PHOTO_ID_RE = re.compile(r'jogadores/[^"\')]*?(\d+)_[a-z]')


def parse_team_logo(html: str, team_id: str) -> str | None:
    """The team crest URL from the team page. zerozero names crest files
    logos/equipas/<team_id>_<slug>_<ts>.png, so matching on this team's id
    isolates its own badge from any other club logos on the page (fixtures
    sidebar, etc.). Works whether the URL sits in src="...", url(...), or a
    data-* attribute; normalises protocol-relative and root-relative forms."""
    m = re.search(
        r'''["'(]\s*([^"')\s]*logos/equipas/''' + re.escape(str(team_id))
        + r'''_[^"')\s]*)\s*["')]''',
        html, re.I)
    if not m:
        return None
    url = m.group(1)
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def team_url(team_id: str, epoca_id: str | None, slug: str = "x", *,
             edicao_id: str | None = None) -> str:
    u = f"{BASE}/equipa/{slug or 'x'}/{team_id}"
    # National-team squads are scoped by competition EDITION (edicao_id) — the
    # /equipa page's "Competição" dropdown — not by club season (epoca_id). When
    # an edicao_id is given it wins: it pins the page to that tournament's squad,
    # which stays correct even after the team's default squad later changes.
    if edicao_id:
        return u + f"?edicao_id={edicao_id}"
    return u + (f"?epoca_id={epoca_id}" if epoca_id else "")


def parse_team_squad(html: str) -> list[dict]:
    """Parse the squad from a team page into player dicts grouped by group.

    Walks the <div class="section"> headers; collects players only from the four
    canonical squad sections (Guarda Redes / Defesa / Médio / Avançado), so the
    staff and honours sections lower down are naturally excluded.
    """
    sections = list(SECTION_RE.finditer(html))
    players: dict[str, dict] = {}
    for i, sec in enumerate(sections):
        group = canon_group(sec.group(1))
        if not group:
            continue
        seg = html[sec.end():(sections[i + 1].start()
                              if i + 1 < len(sections) else len(html))]
        for chunk in STAFF_SPLIT_RE.split(seg)[1:]:
            departed = chunk.startswith(" inactive")
            pm = STAFF_PLAYER_RE.search(chunk)
            if pm:
                slug, pid, name = pm.groups()
            else:
                # Player link without a numeric id: recover the id from the photo.
                nm = STAFF_PLAYER_NOID_RE.search(chunk)
                ph = STAFF_PHOTO_ID_RE.search(chunk)
                if not nm or not ph:
                    continue
                slug, name = nm.groups()
                pid = ph.group(1)
            if pid in players:
                continue
            num = STAFF_NUMBER_RE.search(chunk)
            age = STAFF_AGE_RE.search(chunk)
            photo = STAFF_PHOTO_RE.search(chunk)
            players[pid] = {
                "id": pid,
                "slug": slug,
                "name": crawler.clean_name(name),
                "position_group": group,
                "shirt_number": int(num.group(1)) if num and num.group(1) else None,
                "age": int(age.group(1)) if age else None,
                "photo_url": photo.group(2) if photo else None,
                "source_url": f"{BASE}/jogador/{slug}/{pid}",
                "departed": departed,
            }
    return list(players.values())


def get_team_roster(epoca_id: str | None, team: dict, *,
                    edicao_id: str | None = None, session=None,
                    delay: float = 1.0) -> dict:
    """Stage B — the team's squad for this competition-season.

    `team` is {id, name, slug?}. Returns {team_id, source_url, players:[...]}.
    position_group comes from the roster grouping (authoritative per season);
    age & shirt number are read inline; the detailed position is filled by C.
    Pass `edicao_id` for national-team competitions (see team_url).
    """
    session = session or crawler.new_session()
    url = team_url(team["id"], epoca_id, team.get("slug") or "x",
                   edicao_id=edicao_id)
    html = crawler.fetch(session, url, delay=delay)
    players = parse_team_squad(html)
    logo_url = parse_team_logo(html, team["id"])
    if not players:
        print(f"  ! WARNING: no players parsed for team {team.get('name') or team['id']} "
              f"({url}). Check SECTION_RE/staff parsing (# VERIFY).", file=sys.stderr)
    return {"team_id": team["id"], "source_url": url, "players": players,
            "logo_url": logo_url}


# ----------------------------------------------------------------------------
# Stage C — player detail page: authoritative "Posição", age, club (FULL sync)
# ----------------------------------------------------------------------------

# Name from the page <title>: "Gustavo Klismahn :: 2025/2026 - Santa Clara - ...".
PLAYER_NAME_RE = re.compile(r'<title>\s*([^<:]+?)\s*::')
# After the "Posição" label, the value is the next card-data__value span
# (verified: "Guarda Redes", "Médio Centro", "Ponta de Lança", ...).
POSICAO_RE = re.compile(
    r'card-data__label">\s*Posi\S*o\s*</span>.*?card-data__value">\s*([^<]+?)\s*</span>',
    re.S)
# Birth date + age live in the "Nascimento" card value: "1999-11-23 (26 anos)".
NASCIMENTO_RE = re.compile(
    r'Nascimento\s*</span>\s*<span class="card-data__value">\s*'
    r'(\d{4}-\d{2}-\d{2})(?:\s*\((\d{1,2})\s*anos\))?')
AGE_FALLBACK_RE = re.compile(r'(\d{1,2})\s*anos', re.I)
NAT_RE = re.compile(r'<a title="([^"]+)" href="/pais/')


def parse_player_detail(html: str, pid: str | None, url: str) -> dict:
    name_m = PLAYER_NAME_RE.search(html)
    pos_m = POSICAO_RE.search(html)
    nasc_m = NASCIMENTO_RE.search(html)
    nat_m = NAT_RE.search(html)

    position = crawler.clean_name(pos_m.group(1)) if pos_m else None
    birth_date = nasc_m.group(1) if nasc_m else None
    if nasc_m and nasc_m.group(2):
        age = int(nasc_m.group(2))
    else:
        age_m = AGE_FALLBACK_RE.search(html)
        age = int(age_m.group(1)) if age_m else None

    detail = {
        "id": pid,
        "name": crawler.clean_name(name_m.group(1)) if name_m else None,
        "position": position,
        "position_code": position_code(position),
        "age": age,
        "birth_date": birth_date,
        "nationality": crawler.clean_name(nat_m.group(1)) if nat_m else None,
        "source_url": url,
    }
    if position is None:
        print(f"  ? no 'Posição' parsed for player {pid} ({url}) — POSICAO_RE "
              "may need adjusting (# VERIFY).", file=sys.stderr)
    return detail


def get_player_detail(player: str | dict, *, session=None,
                      delay: float = 1.0) -> dict:
    """Stage C — enrich one player from the detail page. `player` may be an id,
    a /jogador/... URL, or a dict with id/slug."""
    session = session or crawler.new_session()
    if isinstance(player, dict):
        pid = player["id"]
        url = player.get("source_url") or f"{BASE}/jogador/{player.get('slug','x')}/{pid}"
    elif str(player).startswith("http"):
        url = player
        m = re.search(r"/jogador/[^/]+/(\d+)", url)
        pid = m.group(1) if m else None
    else:
        pid = str(player)
        url = f"{BASE}/jogador/x/{pid}"  # slug-agnostic; zerozero resolves by id
    html = crawler.fetch(session, url, delay=delay)
    return parse_player_detail(html, pid, url)


# ----------------------------------------------------------------------------
# Composer — crawl one competition's squads (teams provided by the caller)
# ----------------------------------------------------------------------------


def crawl_competition_squads(comp: dict, teams: list[dict], *, full: bool = False,
                             delay: float = 1.0, enrich_filter=None) -> dict:
    """Fast (full=False) or Full (full=True) squad crawl. `teams` is the team set
    (from roster_sync.team_ids_from_matches): [{id, name, slug?}].

    `enrich_filter(player_id)->bool` skips already-fresh players on a Full sync.
    """
    session = crawler.new_session()
    comp = augment_competition(comp, session=session, delay=delay)
    epoca = comp.get("epoca_id")
    # National-team competitions have no epoca_id; address their squads by the
    # competition edition instead (see team_url).
    edicao = comp.get("id_edicao") if not epoca else None
    print(f"{comp.get('full_name') or comp.get('name')} "
          f"({'edição ' + edicao if edicao else 'época ' + str(epoca)}): "
          f"{len(teams)} team(s).", file=sys.stderr)

    out_teams: list[dict] = []
    enriched: dict[str, dict] = {}
    for i, t in enumerate(teams, 1):
        print(f"[{i}/{len(teams)}] roster: {t.get('name') or t['id']}", file=sys.stderr)
        jobstatus.report(f"Fetching squad — {t.get('name') or t['id']}",
                         current=i, total=len(teams))
        try:
            squad = get_team_roster(epoca, t, edicao_id=edicao,
                                    session=session, delay=delay)
        except Exception as err:  # noqa: BLE001 - skip a bad team, keep going
            print(f"  ! roster failed for {t.get('name') or t['id']}: {err}",
                  file=sys.stderr)
            squad = {"team_id": t["id"],
                     "source_url": team_url(t["id"], epoca, edicao_id=edicao),
                     "players": [], "logo_url": None}
        t = dict(t)
        t["players"] = squad["players"]
        t["source_url"] = squad["source_url"]
        if squad.get("logo_url"):
            t["logo_url"] = squad["logo_url"]
        out_teams.append(t)

        if full:
            for p in squad["players"]:
                if enrich_filter and not enrich_filter(p["id"]):
                    continue
                try:
                    enriched[p["id"]] = get_player_detail(p, session=session,
                                                          delay=delay)
                except Exception as err:  # noqa: BLE001
                    print(f"  ! enrich failed for {p.get('name')}: {err}",
                          file=sys.stderr)
                # No explicit sleep: crawler.fetch() throttles globally.

    return {"competition": comp, "teams": out_teams,
            "enriched_players": enriched, "mode": "full" if full else "fast"}


# ----------------------------------------------------------------------------
# CLI (standalone testing — DB-free; for a single team or player)
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--competition", default=crawler.COMPETITION_URL,
                    help="Competition slug or landing-page URL.")
    ap.add_argument("--team", help="Crawl one team id's roster.")
    ap.add_argument("--player", help="Print one player's detail JSON and exit.")
    ap.add_argument("--full", action="store_true",
                    help="With --team, also enrich each player.")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--out", help="Output JSON file (default stdout).")
    args = ap.parse_args()

    if args.player:
        print(json.dumps(get_player_detail(args.player, delay=args.delay),
                         ensure_ascii=False, indent=2))
        return 0

    url = args.competition if args.competition.startswith("http") \
        else f"{BASE}/competicao/{args.competition}"
    comp = augment_competition(crawler.get_competition(url, delay=args.delay),
                               delay=args.delay)
    if not args.team:
        print("Provide --team <id> (full competition crawl is driven by "
              "roster_sync.py, which discovers teams from the DB).",
              file=sys.stderr)
        return 2

    epoca = comp.get("epoca_id")
    edicao = comp.get("id_edicao") if not epoca else None
    roster = get_team_roster(epoca, {"id": args.team}, edicao_id=edicao,
                             delay=args.delay)
    if args.full:
        for p in roster["players"]:
            p["detail"] = get_player_detail(p, delay=args.delay)
    data = {"competition": comp, "roster": roster}
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(f"\nWrote {args.out}.", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
