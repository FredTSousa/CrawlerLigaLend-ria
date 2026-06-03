#!/usr/bin/env python3
"""
Crawler for zerozero.pt — Liga Portugal results & per-player stats.

A three-stage, composable pipeline (use the stages directly or via the CLI):

    1. get_competition()      -> the variables needed to request any fixture
                                 (fase, id_edicao, the list of rounds, ...)
    2. get_fixture(comp, n)   -> every match of round n, each with its link
    3. get_match(game)        -> the JSON for one match

Typical use:

    import crawler
    comp = crawler.get_competition()
    fixture = crawler.get_fixture(comp, 31)
    for game in fixture["games"]:
        data = crawler.get_match(game)
        ...

For each match it reads every player who actually played (i.e. is NOT greyed
out) and extracts:

    goals, assists, yellow_cards, red_card, own_goals,
    penalties_scored, penalties_missed, penalties_defended, played_under_20m

plus the final result. Teams and players are emitted as {name, id}.

Only the Python standard library + `requests` are required.
"""

from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

import jobstatus  # best-effort progress heartbeat for the runner tray

# zerozero.pt's anti-bot layer fingerprints the TLS handshake; the stdlib
# `requests` fingerprint gets a 403 from flagged IPs. curl_cffi impersonates a
# real Chrome handshake, which gets through. Fall back to plain requests if it
# isn't installed (e.g. quick local runs).
try:
    from curl_cffi import requests as cffi_requests  # type: ignore
    _CFFI_OK = True
except Exception:  # noqa: BLE001
    cffi_requests = None
    _CFFI_OK = False

# curl_cffi browser profile to impersonate. Override via $IMPERSONATE.
IMPERSONATE = os.environ.get("IMPERSONATE", "chrome")

BASE = "https://www.zerozero.pt"
COMPETITION_URL = f"{BASE}/competicao/liga-portuguesa"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

# Global request throttle. zerozero classifies fast/bursty traffic as a bot and
# answers with *poisoned* HTTP-200 pages (random teams, random scores) rather
# than a 403 — so politeness is the only safe strategy. Every zerozero request
# (match crawl AND roster crawl) goes through fetch(), so enforcing a minimum
# gap here throttles the whole crawler from one place, regardless of caller.
#
# Tunable without code changes:
#   ZEROZERO_MIN_INTERVAL  seconds between requests (floor; default 5.0)
#   ZEROZERO_JITTER        extra random 0..N seconds per request (default 3.0)
# Set ZEROZERO_MIN_INTERVAL=0 to disable (e.g. fast local parsing tests).
MIN_REQUEST_INTERVAL = float(os.environ.get("ZEROZERO_MIN_INTERVAL", "5.0"))
REQUEST_JITTER = float(os.environ.get("ZEROZERO_JITTER", "3.0"))

# Wall-clock (monotonic) timestamp of the last request, shared process-wide so a
# single crawl run paces all its requests together.
_last_request_ts = 0.0


def _throttle(min_interval: float) -> None:
    """Block until at least `min_interval` (+ a little random jitter) seconds
    have elapsed since the previous request. Jitter avoids a robotically exact
    cadence, which is itself a bot signal."""
    global _last_request_ts
    interval = max(min_interval, 0.0)
    if interval <= 0 and REQUEST_JITTER <= 0:
        _last_request_ts = time.monotonic()
        return
    target = interval + (random.uniform(0, REQUEST_JITTER) if REQUEST_JITTER > 0 else 0)
    wait = (_last_request_ts + target) - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.monotonic()


def new_session():
    """Create a scraping session. Prefers curl_cffi (Chrome-impersonating TLS)
    so flagged IPs get past zerozero's 403; falls back to stdlib requests."""
    if _CFFI_OK:
        try:
            return cffi_requests.Session(impersonate=IMPERSONATE)
        except Exception:  # noqa: BLE001 - bad profile name, etc.
            pass
    return requests.Session()


def fetch(session, url: str, *, retries: int = 3, delay: float = 1.0) -> str:
    """GET a URL with a polite delay and a few retries; return the HTML text."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        # Space every request (incl. retries) by at least the global minimum;
        # an explicit larger `delay` raises the floor.
        _throttle(max(delay, MIN_REQUEST_INTERVAL))
        try:
            if _CFFI_OK:
                # Let the impersonated profile own the headers/ordering;
                # only nudge the language.
                resp = session.get(
                    url, headers={"Accept-Language": HEADERS["Accept-Language"]},
                    timeout=30)
            else:
                resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            # zerozero declares Windows-1252 (e.g. "Trincão" is byte 0xE3, not
            # UTF-8). Respect the response's declared encoding; only guess when
            # none is given. (Forcing utf-8 here corrupts accents to U+FFFD.)
            try:
                if not resp.encoding:
                    resp.encoding = (getattr(resp, "apparent_encoding", None)
                                     or "utf-8")
            except Exception:  # noqa: BLE001
                pass
            return resp.text
        except Exception as err:  # noqa: BLE001 - retry on any transport error
            last_err = err
            print(f"  ! fetch failed ({attempt}/{retries}) {url}: {err}",
                  file=sys.stderr)
            time.sleep(delay * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last_err}")


# ----------------------------------------------------------------------------
# Small parsing helpers
# ----------------------------------------------------------------------------

GAME_LINK_RE = re.compile(r"/jogo/([0-9]{4}-[0-9]{2}-[0-9]{2}-[a-z0-9-]+)/(\d+)")
TEAM_LINK_RE = re.compile(r'/equipa/([a-z0-9-]+)/(\d+)')
PLAYER_LINK_RE = re.compile(r'/jogador/([a-z0-9-]+)/(\d+)">([^<]+)</a>')
TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", html)).strip()


def clean_name(raw: str) -> str:
    """Decode HTML entities (&nbsp;, &amp; ...) and normalise whitespace."""
    return re.sub(r"\s+", " ", htmllib.unescape(raw).replace("\xa0", " ")).strip()


def leading_minute(text: str) -> int | None:
    """Extract the base minute from strings like "71'", "90+1'", "66' (g.p.)"."""
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


# ----------------------------------------------------------------------------
# Competition (round) page -> list of game URLs
# ----------------------------------------------------------------------------


# Visible team name (home cell, then away cell — both class="text").
TEAM_NAME_RE = re.compile(
    r'<td class="text"[^>]*>\s*<a href="/equipa/[^"]+">'
    r'(?:<b>)?\s*([^<]+?)\s*(?:</b>)?</a>')
# Team id is reliably encoded in the crest image filename, even when the
# anchor itself omits it (e.g. /equipa/benfica?epoca_id=155).
TEAM_LOGO_ID_RE = re.compile(
    r'<a href="/equipa/[^"]*">\s*<img[^>]*logos/equipas/(\d+)_')
RESULT_CELL_RE = re.compile(
    r'<td id="tdl_(\d+)" class="result">(.*?)</td>', re.S)


def parse_round_games(html: str) -> list[dict]:
    """Return full fixture info for every game in the round's table.

    Each item: {game_id, slug, url, date, home_team{name,id},
                away_team{name,id}, result{home,away}|None}.
    The fixtures table is the authoritative source for team name+id and the
    final score (some game-page header links omit the numeric team id).
    """
    start = html.find('id="fixture_games"')
    if start == -1:
        raise RuntimeError("Could not find the fixtures table (#fixture_games).")
    end = html.find("</table>", start)
    block = html[start:end if end != -1 else len(html)]

    games: list[dict] = []
    seen: set[str] = set()
    for row in block.split("<tr>")[1:]:
        res = RESULT_CELL_RE.search(row)
        if not res:
            continue
        gid = res.group(1)
        if gid in seen:
            continue
        seen.add(gid)

        link = GAME_LINK_RE.search(row)
        slug = link.group(1) if link else gid

        # Names: home text cell precedes the result td, away cell follows it.
        # Ids: home crest precedes the result td, away crest follows it.
        names = TEAM_NAME_RE.findall(row)
        ids = TEAM_LOGO_ID_RE.findall(row)
        if len(names) < 2 or len(ids) < 2:
            continue
        home = {"name": clean_name(names[0]), "id": ids[0]}
        away = {"name": clean_name(names[1]), "id": ids[1]}

        score = None
        sm = re.search(r"(\d+)\s*-\s*(\d+)", strip_tags(res.group(2)))
        if sm:
            score = {"home": int(sm.group(1)), "away": int(sm.group(2))}

        date_m = re.match(r"(\d{4}-\d{2}-\d{2})", slug)
        games.append({
            "game_id": gid,
            "slug": slug,
            "url": f"{BASE}/jogo/{slug}/{gid}",
            "date": date_m.group(1) if date_m else None,
            "home_team": home,
            "away_team": away,
            "result": score,
        })
    return games


# ----------------------------------------------------------------------------
# Game page -> header (teams + result)
# ----------------------------------------------------------------------------


def parse_game_header(html: str) -> dict:
    """Extract home/away team {name,id} and the final score from a game page.

    Works even when a team's link omits the numeric id (e.g. big clubs):
    the name comes from the result <h1> (home = "right", away = "left") and
    the id from the header crest image filename (logos/equipas/<id>_...).
    """
    h1 = re.search(r'<h1 class="match-header-result">(.*?)</h1>', html, re.S)
    block = h1.group(1) if h1 else html

    def team_name(side: str) -> str | None:
        m = re.search(
            r'match-header-team ' + side + r'.*?/equipa/[^"]*">([^<]+)',
            block, re.S)
        return clean_name(m.group(1)) if m else None

    def crest_id(container: str) -> str | None:
        m = re.search(r'class="' + container + r'".*?logos/equipas/(\d+)_',
                      html, re.S)
        return m.group(1) if m else None

    # The home crest sits in .match-header-left, the away crest in
    # .match-header-right (the team-NAME blocks are right=home, left=away).
    home_name, away_name = team_name("right"), team_name("left")
    home_id, away_id = crest_id("match-header-left"), crest_id("match-header-right")

    def team(name: str | None, tid: str | None) -> dict | None:
        if not name:
            return None
        return {"name": name, "id": tid}

    home = team(home_name, home_id)
    away = team(away_name, away_id)

    score = None
    vs = re.search(r'<div class="match-header-vs"[^>]*>(.*?)</div>', block, re.S)
    if vs:
        sm = re.search(r"(\d+)\s*-\s*(\d+)", strip_tags(vs.group(1)))
        if sm:
            score = {"home": int(sm.group(1)), "away": int(sm.group(2))}

    return {"home_team": home, "away_team": away, "result": score}


def parse_game_status(html: str, result: dict | None) -> dict:
    """Best-effort match state: {status, minute, kickoff_at}.

    Conservative on purpose: zerozero renders "0-0" pre-game and injects the
    live minute client-side, so the static HTML can't reliably distinguish
    pre-game / live / final. We therefore default to 'scheduled' here and let
    round crawls infer 'final' from the fixtures score (see sync._match_row).
    Live/final detection from an in-progress page is wired in during a live
    match, when the real markup can be inspected.
    """
    return {"status": "scheduled", "minute": None, "kickoff_at": None}


# ----------------------------------------------------------------------------
# Game page -> lineup & per-player events
# ----------------------------------------------------------------------------

SUBTITLE_RE = re.compile(r'<div class="subtitle">([^<]+)</div>')
# One event = an icon <span> immediately followed by its minute <div>.
EVENT_RE = re.compile(
    r'<span([^>]*?)>([^<]*)</span>\s*<div>([^<]*)</div>')
TITLE_RE = re.compile(r'title="([^"]*)"')
CLASS_RE = re.compile(r'class="([^"]*)"')
# Each scoring/assist minute within an event div, with any trailing annotation.
# A brace renders as a single icon + one div listing every minute, e.g.
#   "89' 90+1'"  -> two goals
#   "31' 72' (g.p.)"  -> goal at 31', penalty at 72'
#   "17' (g.p.) 37'"  -> penalty at 17', goal at 37'
MINUTE_TOKEN_RE = re.compile(r"(\d+(?:\+\d+)?)'\s*((?:\([^)]*\)\s*)*)")


def classify_events(events_html: str) -> dict:
    """Turn a player's events HTML into the required stat counters."""
    stats = {
        "goals": 0,
        "assists": 0,
        "yellow_cards": 0,
        "red_card": False,
        "own_goals": 0,
        "penalties_scored": 0,
        "penalties_missed": 0,
        "penalties_defended": 0,
    }
    entered_min: int | None = None
    left_min: int | None = None
    raw_events: list[dict] = []
    unknown: list[dict] = []

    # 1. Broadly check for any visual indicator of a red card
    if ('title="Vermelhos"' in events_html
            or re.search(r'class="icn_zerozero[^"]*\bred\b', events_html)):
        stats["red_card"] = True

    # 2. Loop #2: Extract every single icon span and assign stats cleanly
    SPAN_RE = re.compile(r'<span([^>]*?)>([^<]*)</span>')
    all_minutes = [leading_minute(m) for m in re.findall(r'<div>([^<]*)</div>', events_html)]

    for attrs, glyph in SPAN_RE.findall(events_html):
        title_m = TITLE_RE.search(attrs)
        class_m = CLASS_RE.search(attrs)
        title = (title_m.group(1) if title_m else "").strip()
        klass = (class_m.group(1) if class_m else "").strip()
        title_l = title.lower()

        raw_events.append({"title": title, "class": klass, "glyph": glyph.strip()})

        # Match goals (cleared and recalibrated precisely via companion strings in Loop #3)
        if "zz-icn-fut" in klass or title == "Golos":
            pass 
        
        # Match Assists
        elif title == "Assistência" or "assist" in title_l:
            stats["assists"] += 1
            
        # Match Yellow Cards
        elif title == "Amarelos" or "yellow" in klass:
            stats["yellow_cards"] += 1
            
        # Match Red Cards
        elif title == "Vermelhos" or "red" in klass:
            stats["red_card"] = True
            
        # Match Penalties (Authoritative placement - evaluated once per icon here)
        elif title == "Penaltis Falhados" or "falhad" in title_l:
            stats["penalties_missed"] += 1

        elif title == "Penaltis Defendidos" or "defendid" in title_l or "defes" in title_l:
            stats["penalties_defended"] += 1
            
        elif title == "Entrou" or "entrou" in title_l:
            if all_minutes: entered_min = all_minutes[-1]
            
        # Structural annotations
        elif not title or any(x in klass for x in ["grey", "green", "red"]):
            pass
            
        else:
            unknown.append({"title": title, "class": klass, "glyph": glyph})

    # 3. Loop #3: Double-check complex text strings (ONLY for Goals and Subs)
    for attrs, glyph, minute_text in EVENT_RE.findall(events_html):
        klass = (CLASS_RE.search(attrs).group(1) if CLASS_RE.search(attrs) else "").strip()
        title = (TITLE_RE.search(attrs).group(1) if TITLE_RE.search(attrs) else "").strip()
        minute = leading_minute(minute_text)
        title_l = title.lower()
        
        if "zz-icn-fut" in klass or title == "Golos":
            tokens = MINUTE_TOKEN_RE.findall(minute_text) or [("", "")]
            stats["goals"] = 0 
            for _min, mann in tokens:
                a = mann.lower()
                if "p.b." in a or "auto" in a:
                    stats["own_goals"] += 1
                elif "g.p." in a or "grande penal" in a:
                    # Penalty goal: counted separately, NOT as an open-play goal.
                    stats["penalties_scored"] += 1
                else:
                    stats["goals"] += 1
                        
        elif title == "Entrou" or "entrou" in title_l:
            entered_min = minute
            
        elif not title and "grey" in klass and minute is not None:
            left_min = minute

    # Cascade rules
    if stats["yellow_cards"] >= 2:
        stats["red_card"] = True

    played_under_20m = (
        (entered_min is not None and entered_min >= 70) or
        (left_min is not None and left_min < 20)
    )
    stats["played_under_20m"] = played_under_20m

    return {
        "stats": stats,
        "entered_min": entered_min,
        "left_min": left_min,
        "raw_events": raw_events,
        "unknown_events": unknown,
    }


def parse_player_block(chunk: str) -> dict | None:
    """Parse a single player chunk (already split on `<div class="player`)."""
    inactive = chunk.startswith(" inactive")
    pl = PLAYER_LINK_RE.search(chunk)
    if not pl:
        return None
    player_id = pl.group(2)
    name = clean_name(pl.group(3))
    captain = name.endswith("(C)")
    if captain:
        name = name[:-3].strip()

    num_m = re.search(r'data-player-id="\d+"[^>]*>([^<]*)</div>', chunk)
    number = None
    if num_m:
        n = num_m.group(1).strip()
        number = int(n) if n.isdigit() else None

    ev_m = re.search(r'<div class="events">(.*)', chunk, re.S)
    events_html = ev_m.group(1) if ev_m else ""
    parsed = classify_events(events_html)

    return {
        "name": name,
        "id": player_id,
        "number": number,
        "captain": captain,
        "inactive": inactive,
        **parsed,
    }


def parse_lineups(html: str, home_team: dict, away_team: dict) -> list[dict]:
    """Return all players that actually played, tagged with team + role."""
    subs = list(SUBTITLE_RE.finditer(html))
    if len(subs) < 2:
        return []

    # Boundaries: [home_starters, away_starters, suplentes, suplentes, Treinadores...]
    # Identify the index where "Treinadores" begins to stop there.
    stop_idx = len(html)
    seg_starts: list[tuple[int, int, str]] = []  # (start, end, subtitle_text)
    for i, m in enumerate(subs):
        text = m.group(1).strip()
        seg_start = m.end()
        seg_end = subs[i + 1].start() if i + 1 < len(subs) else len(html)
        if text.lower().startswith("treinador"):
            stop_idx = m.start()
            break
        seg_starts.append((seg_start, seg_end, text))

    # Expect first 4 segments: home XI, away XI, suplentes(home), suplentes(away)
    # Map by order, cross-checking starter subtitles against header team names.
    if len(seg_starts) < 2:
        return []

    first_name = seg_starts[0][2]
    home_first = _name_matches(first_name, home_team["name"])

    role_map: list[tuple[tuple[int, int], dict, bool]] = []
    # starters
    starters = seg_starts[:2]
    suplentes = seg_starts[2:4]
    a_team, b_team = (home_team, away_team) if home_first else (away_team, home_team)
    if starters:
        role_map.append(((starters[0][0], starters[0][1]), a_team, True))
    if len(starters) > 1:
        role_map.append(((starters[1][0], starters[1][1]), b_team, True))
    if len(suplentes) > 0:
        role_map.append(((suplentes[0][0], suplentes[0][1]), a_team, False))
    if len(suplentes) > 1:
        role_map.append(((suplentes[1][0], suplentes[1][1]), b_team, False))

    players: list[dict] = []
    for (seg_a, seg_b), team, is_starter in role_map:
        seg_html = html[seg_a:min(seg_b, stop_idx)]
        for chunk in _split_players(seg_html):
            p = parse_player_block(chunk)
            if not p:
                continue
            if p["inactive"]:
                continue  # greyed-out: did not play
            players.append({
                "name": p["name"],
                "id": p["id"],
                "team": {"name": team["name"], "id": team["id"]},
                "number": p["number"],
                "captain": p["captain"],
                "starter": is_starter,
                "minutes": {"entered": p["entered_min"], "left": p["left_min"]},
                "stats": p["stats"],
                "_unknown_events": p["unknown_events"],
            })
    return players


def _split_players(seg_html: str) -> list[str]:
    """Split a segment into per-player chunks on `<div class="player` markers."""
    marker = '<div class="player'
    parts = seg_html.split(marker)
    return [p for p in parts[1:]]  # parts[0] is text before the first player


def _name_matches(a: str, b: str) -> bool:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    return norm(a) == norm(b) or norm(a) in norm(b) or norm(b) in norm(a)


# ----------------------------------------------------------------------------
# Public pipeline — three composable stages
#
#   1. get_competition()  -> the variables needed to request any fixture
#   2. get_fixture(...)    -> every match of a round, each with its link
#   3. get_match(...)      -> the JSON for a single match
# ----------------------------------------------------------------------------

ID_EDICAO_RE = re.compile(r'name="id_edicao"[^>]*value="(\d+)"')
FASE_RE = re.compile(r'name="fase"[^>]*value="(\d+)"')
ROUND_OPTION_RE = re.compile(
    r'<option value="(\d+)"([^>]*)>\s*Jornada\s*\d+\s*</option>')


def _session(session: requests.Session | None) -> requests.Session:
    return session if session is not None else new_session()


def _competition_name(html: str) -> str | None:
    m = re.search(r"<title>([^<]*)</title>", html)
    return m.group(1).split(" - ")[0].strip() if m else None


def _competition_slug(url: str) -> str | None:
    """The zerozero slug from a competition URL (…/competicao/<slug>[?…])."""
    m = re.search(r"/competicao/([a-z0-9-]+)", url)
    return m.group(1) if m else None


def get_competition(base_url: str = COMPETITION_URL, *,
                    session: requests.Session | None = None,
                    delay: float = 1.0) -> dict:
    """Stage 1 — read the competition landing page and return the variables
    needed to request each fixture (round).

    Returns: {name, url, id_edicao, fase, rounds: [int], current_round: int}.
    Use ``fixture_url(comp["fase"], n)`` (or pass ``comp`` to ``get_fixture``)
    to address round ``n``.
    """
    session = _session(session)
    html = fetch(session, base_url, delay=delay)

    fase_m = FASE_RE.search(html)
    edi_m = ID_EDICAO_RE.search(html)
    options = ROUND_OPTION_RE.findall(html)
    rounds = sorted({int(v) for v, _ in options})
    selected = [int(v) for v, attr in options if "selected" in attr]
    current = selected[-1] if selected else (max(rounds) if rounds else None)

    if not fase_m or not rounds:
        raise RuntimeError(f"Could not read competition variables from {base_url}")

    return {
        "name": _competition_name(html),
        "url": base_url,
        "slug": _competition_slug(base_url),
        "id_edicao": edi_m.group(1) if edi_m else None,
        "fase": fase_m.group(1),
        "rounds": rounds,
        "current_round": current,
    }


def fixture_url(fase: str | int, jornada: int | str,
                base_url: str = COMPETITION_URL) -> str:
    """Build the URL of a single round (fixture)."""
    return f"{base_url}?jornada_in={jornada}&fase={fase}"


def get_fixture(competition: dict | str | int, jornada: int | str, *,
                session: requests.Session | None = None,
                delay: float = 1.0) -> dict:
    """Stage 2 — list every match of a round, each with its link and meta.

    ``competition`` may be the dict from :func:`get_competition` or a bare
    ``fase`` value. Returns:
        {round, url, games: [{game_id, slug, url, date,
                              home_team{name,id}, away_team{name,id}, result}]}
    """
    fase = competition["fase"] if isinstance(competition, dict) else competition
    # Address the round on the competition's OWN landing page, so non-default
    # leagues build the right URL (the bare-fase form falls back to liga).
    base = (competition.get("url") if isinstance(competition, dict) else None) \
        or COMPETITION_URL
    session = _session(session)
    url = fixture_url(fase, jornada, base)
    html = fetch(session, url, delay=delay)
    games = parse_round_games(html)
    return {"round": int(jornada), "url": url, "games": games}


def get_match(game: dict | str, *,
              session: requests.Session | None = None,
              delay: float = 1.0) -> dict:
    """Stage 3 — return the full JSON for a single match.

    ``game`` may be a dict produced by :func:`get_fixture` (preferred — its
    team ids/result are authoritative) or a bare match URL (the teams and
    result are then read from the match page itself).
    """
    session = _session(session)
    seed = {} if isinstance(game, str) else dict(game)
    url = game if isinstance(game, str) else game["url"]

    html = fetch(session, url, delay=delay)
    header = parse_game_header(html)

    home = seed.get("home_team") or header["home_team"]
    away = seed.get("away_team") or header["away_team"]
    result = seed.get("result") or header.get("result")
    if not home or not away:
        raise RuntimeError(f"Could not determine teams for {url}")

    players = parse_lineups(html, home, away)
    state = parse_game_status(html, result)

    # surface any unmapped event types so the icon vocabulary can be extended
    unknown = [u for p in players for u in p.pop("_unknown_events", [])]
    if unknown:
        print(f"  ? unmapped events in {url}: "
              f"{json.dumps(unknown, ensure_ascii=False)}", file=sys.stderr)

    link = GAME_LINK_RE.search(url)
    return {
        "game_id": seed.get("game_id") or (link.group(2) if link else None),
        "url": url,
        "date": seed.get("date") or (link.group(1)[:10] if link else None),
        "home_team": home,
        "away_team": away,
        "result": result,
        "status": state["status"],
        "minute": state["minute"],
        "kickoff_at": state["kickoff_at"],
        "players": players,
    }


# ----------------------------------------------------------------------------
# Convenience composer — runs all three stages for one round
# ----------------------------------------------------------------------------


def crawl_round(jornada: int | str | None = None, *,
                competition: dict | None = None,
                round_url: str | None = None,
                delay: float = 1.0) -> dict:
    """Crawl one full round end-to-end (composes the three stages).

    Provide a ``round_url`` directly, or a ``jornada`` number (the competition
    variables are resolved automatically unless ``competition`` is supplied).
    """
    session = new_session()

    if round_url:
        jm = re.search(r"jornada_in=(\d+)", round_url)
        jornada = int(jm.group(1)) if jm else jornada
        html = fetch(session, round_url, delay=delay)
        competition = competition or {"name": _competition_name(html)}
        fixture = {"round": jornada, "url": round_url,
                   "games": parse_round_games(html)}
    else:
        competition = competition or get_competition(session=session, delay=delay)
        if jornada is None:
            jornada = competition["current_round"]
        fixture = get_fixture(competition, jornada, session=session, delay=delay)

    print(f"Round {fixture['round']}: {len(fixture['games'])} games.",
          file=sys.stderr)

    games: list[dict] = []
    total = len(fixture["games"])
    for i, g in enumerate(fixture["games"], 1):
        label = g.get("slug", g["url"])
        print(f"[{i}/{total}] {label}", file=sys.stderr)
        jobstatus.report(f"Round {fixture['round']}: match {label}",
                         current=i, total=total)
        try:
            games.append(get_match(g, session=session, delay=delay))
        except Exception as err:  # noqa: BLE001
            print(f"  ! skipped {g['url']}: {err}", file=sys.stderr)
        # No explicit sleep: fetch() throttles every request globally.

    return {
        "competition": competition.get("name") if competition else None,
        "round": fixture["round"],
        "source_url": fixture["url"],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "games": games,
    }


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--jornada", type=int,
                      help="Round number to crawl (resolves fase via the "
                           "competition page). Defaults to the current round.")
    mode.add_argument("--url",
                      help="A specific round URL (with jornada_in & fase).")
    mode.add_argument("--match",
                      help="A single match URL — outputs just that match's JSON.")
    ap.add_argument("--base", default=COMPETITION_URL,
                    help="Competition landing-page URL.")
    ap.add_argument("--out", help="Output JSON file (defaults to stdout).")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="Polite delay (seconds) between requests.")
    ap.add_argument("--list-rounds", action="store_true",
                    help="Just print the competition variables and exit.")
    args = ap.parse_args()

    if args.list_rounds:
        comp = get_competition(args.base, delay=args.delay)
        print(json.dumps(comp, ensure_ascii=False, indent=2))
        return 0

    if args.match:
        data = get_match(args.match, delay=args.delay)
    elif args.url:
        data = crawl_round(round_url=args.url, delay=args.delay)
    else:
        data = crawl_round(jornada=args.jornada, delay=args.delay)

    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        n = len(data.get("games", [])) if "games" in data else 1
        print(f"\nWrote {args.out} ({n} game(s)).", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
