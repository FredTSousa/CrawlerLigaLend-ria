#!/usr/bin/env python3
"""
A Bola (abola.pt) player-ratings scraper.

Given finished games (from the zerozero crawl), find the matching A Bola
article(s) via Google and extract per-player ratings ("notas") + the MVP.

Two article formats:
  * Format 1 (non-big-three): ONE "crónica" page with both teams' ratings as an
    inline comma-separated list  ->  Layout A.
  * Format 2 (Sporting/Benfica/Porto): TWO separate "as notas do <team>" pages,
    usually a deep-dive paragraph per player  ->  Layout B.

Both layouts are parsed structurally with BeautifulSoup; the player id comes
from the A Bola autolink (`data-resource-id` / .../jogador/<slug>-<id>).

Requires: requests, beautifulsoup4, lxml, googlesearch-python.

Search is pluggable: set abola.SEARCH to your own `f(query, n) -> [url]` (e.g.
a SerpAPI/Custom Search backend) if Google scraping gets rate-limited. You can
also bypass search entirely by passing URLs to scrape_match().
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}

# Allow same-day or up to a few days after the match for the article to appear.
MAX_DATE_LAG_DAYS = 4


# ----------------------------------------------------------------------------
# Search backend — A Bola's own search at /pesquisar (server-rendered results)
# ----------------------------------------------------------------------------

SEARCH_URL = "https://www.abola.pt/pesquisar?q="
ARTICLE_RE = re.compile(
    r'href="((?:https://www\.abola\.pt)?/(?:futebol/)?noticias/[a-z0-9\-]+-\d{12,})"')


def abola_search(query: str, pages: int = 1,
                 until_date: date | None = None) -> list[tuple[date, str]]:
    """Query abola.pt/pesquisar; return [(article_date, url)] (date-sorted).
    Fetches up to `pages` pages. Results come newest-first, so if `until_date`
    is given, stop paging once a page's newest article predates it (deeper
    pages are only older still) -- lets callers walk back exactly to a match's
    date without a fixed page count."""
    out, seen = [], set()
    for page in range(1, pages + 1):
        url = SEARCH_URL + urllib.parse.quote(query)
        if page > 1:
            url += f"&page={page}"
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as err:  # noqa: BLE001
            # A short result set 404s on later pages -- that's "no more pages",
            # not a real failure; only surface an error on the very first page.
            if page == 1:
                print(f"  ! search failed for {query!r}: {err}", file=sys.stderr)
            break
        found, newest = 0, None
        for h in ARTICLE_RE.findall(r.text):
            u = h if h.startswith("http") else "https://www.abola.pt" + h
            if u in seen:
                continue
            seen.add(u)
            d = url_date(u)
            if d:
                out.append((d, u))
                found += 1
                newest = d if newest is None else max(newest, d)
        if found == 0 and page > 1:
            break  # no more pages
        if until_date and newest is not None and newest < until_date:
            break  # paged back past the window of interest
    return out


# Pluggable: override SEARCH with f(query) -> [(date, url)] if needed.
SEARCH = abola_search


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _norm(s: str) -> str:
    """Lowercase, strip accents and non-alphanumerics (for name matching)."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _slug(team: str) -> str:
    s = unicodedata.normalize("NFKD", team or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def url_date(url: str) -> date | None:
    """A Bola article URLs end with a YYYYMMDDHHMMSS... number."""
    m = re.search(r"(\d{8,})/?$", url)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1)[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _score(raw: str | None):
    """'(5)' -> 5 ; '(-)' / '-' / '' -> None."""
    if raw is None:
        return None
    m = re.search(r"-?\d+", raw)
    return int(m.group(0)) if m else None


def fetch(url: str, *, retries: int = 3, delay: float = 1.0) -> str:
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except Exception as err:  # noqa: BLE001
            last = err
            print(f"  ! fetch {attempt}/{retries} {url}: {err}", file=sys.stderr)
            time.sleep(delay * attempt)
    raise RuntimeError(f"Could not fetch {url}: {last}")


# ----------------------------------------------------------------------------
# Discovery
# ----------------------------------------------------------------------------


# Club-type abbreviations to ignore when picking a team's distinctive token.
CLUB_ABBR = {"fc", "sc", "ac", "cd", "sl", "ad", "gd", "cf", "sad", "avs",
             "ud", "cs", "sg", "praia"}


def _team_tokens(team: str) -> set[str]:
    """Distinctive name tokens for matching a team in a URL / page. A Bola uses
    short forms ('Estoril Praia' -> 'estoril', 'Vitória SC' -> 'vitoria',
    'SC Braga' -> 'braga'), so drop club-type words and keep the rest."""
    toks = {_norm(w) for w in re.split(r"\s+", team.strip()) if w}
    sig = {t for t in toks if t and len(t) >= 3 and t not in CLUB_ABBR}
    return sig or {_norm(team)}


def _clean_team(team: str) -> str:
    """Team name without club-type words, matching A Bola's short form
    ('Estoril Praia' -> 'Estoril', 'Vitória SC' -> 'Vitória')."""
    words = [w for w in re.split(r"\s+", team.strip())
             if _norm(w) not in CLUB_ABBR]
    return " ".join(words) or team


def _in_window(d: date, game_date: date) -> bool:
    return 0 <= (d - game_date).days <= MAX_DATE_LAG_DAYS


def find_team_notas(team: str, game_date: date) -> str | None:
    """Find the 'as notas do <team>' page for a match on/just after game_date."""
    tokens = _team_tokens(team)
    clean = _clean_team(team)
    for q in (f"as notas do {clean}", f"notas {clean}", f"as notas do {team}"):
        cands = [(d, u) for d, u in abola_search(q, pages=2)
                 if _in_window(d, game_date) and "notas" in u
                 and any(t in u.lower() for t in tokens)]
        cands.sort()
        if cands:
            return cands[0][1]
    return None


def find_cronica(home: str, away: str, game_date: date) -> str | None:
    """Find the single 'crónica' page (non-big-three). Crónica titles rarely
    name the teams (and 'Nacional' literally means 'national', so title/slug
    matching gives false hits), so we DON'T filter by title: list every crónica
    in the date window and OPEN each, confirming the parsed teams cover both
    sides. Paging walks back exactly to the game date via `until_date`."""
    ht, at = _team_tokens(home), _team_tokens(away)
    cands: dict[str, date] = {}
    for d, u in abola_search("crónica", pages=15, until_date=game_date):
        if "cronica" in u and _in_window(d, game_date):
            cands[u] = d
    for u in sorted(cands, key=lambda x: cands[x]):
        try:
            page = parse_page(fetch(u))
        except Exception:  # noqa: BLE001
            continue
        names = " ".join(_norm(t) for t in page["teams"])
        if any(t in names for t in ht) and any(t in names for t in at):
            return u
    return None


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

NOTAS_RE = re.compile(r"as notas d", re.I)
# "O melhor em campo: Name (7)" or "A figura [do Team]: Name (nota 6)"
MVP_RE = re.compile(
    r"(o melhor em campo|a figura(?:\s+d[oae]\s+([^:<\n]+?))?)\s*:\s*"
    r"([^()<\n]+?)\s*\(\s*(?:nota\s*)?([\d\-–]+)\s*\)", re.I)


def _mvp_boxes(soup) -> list[dict]:
    """Find 'O melhor em campo' / 'A figura' boxes -> name, score, team, text."""
    boxes, seen = [], set()
    for node in soup.find_all(string=re.compile(r"(melhor em campo|a figura)\s*:", re.I)):
        m = MVP_RE.search(node)
        if not m:
            continue
        title = m.group(0)
        name = m.group(3).strip()
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)
        # narrative: nearest ancestor whose text extends past the title
        narrative, anc = None, node.parent
        for _ in range(5):
            if anc is None:
                break
            full = anc.get_text(" ", strip=True)
            if len(full) > len(title) + 5 and title in full:
                narrative = full.split(title, 1)[1].strip() or None
                break
            anc = anc.parent
        boxes.append({
            "kind": "melhor" if "melhor" in m.group(1).lower() else "figura",
            "team": (m.group(2) or "").strip() or None,
            "name": name,
            "score": _score(m.group(4)),
            "description": narrative,
        })
    return boxes


def _player_id(a) -> str | None:
    if a.has_attr("data-resource-id"):
        return a["data-resource-id"]
    m = re.search(r"/jogador/[^/]*?-(\d+)\b", a.get("href", ""))
    return m.group(1) if m else None


def _team_of_header(strong) -> str | None:
    tl = strong.find("a", attrs={"data-resource-type": "team"})
    if tl:
        return tl.get_text(strip=True)
    # No team autolink (common for the second team's header): parse the name
    # from the header text. The ":" and the "(formation)" suffix often live
    # OUTSIDE the <strong> ("...do Vitória de Guimarães (4x2x3x1)" : Charles…),
    # so stop at the first of "(", ":" or end-of-string -- requiring a trailing
    # colon here would silently drop the team and dump its players elsewhere.
    m = re.search(r"as notas d\w*\s+(?:jogadores\s+d\w*\s+)?(.+?)\s*(?:\(|:|$)",
                  strong.get_text(" ", strip=True), re.I)
    return m.group(1).strip() if m else None


def _layout_a_players(p, strong) -> list[dict]:
    """Inline list: <a player>Name</a> (score), ... after the header strong."""
    players = []
    for a in p.find_all("a", attrs={"data-resource-type": "player"}):
        # gather the text right after this link up to the next link
        txt, sib = "", a.next_sibling
        while sib is not None and getattr(sib, "name", None) != "a":
            txt += sib if isinstance(sib, str) else sib.get_text()
            sib = sib.next_sibling
        sm = re.search(r"\(\s*([\d\-–]+)\s*\)", txt)
        players.append({
            "player_name": a.get_text(strip=True),
            "player_id": _player_id(a),
            "score": _score(sm.group(1) if sm else None),
            "is_mvp": False,
            "description": None,
        })
    return players


def _layout_b_player(p, strong) -> dict | None:
    """Deep dive: <strong>{score} {Name} —</strong> narrative..."""
    a = strong.find("a", attrs={"data-resource-type": "player"})
    if not a:
        return None
    bold = strong.get_text(" ", strip=True)
    # Two score positions seen across A Bola pages:
    #   leading:       "7 Rui Silva —"
    #   parenthetical: "Trubin (8)" / "Tomás Araújo (6)" / "Name (-)"
    name, score = bold, None
    lead = re.match(r"\s*(-?\d+)\b", bold)
    paren = re.search(r"\(\s*([\d\-–]+)\s*\)", bold)
    if lead:
        score = int(lead.group(1))
        name = bold[lead.end():]
    elif paren:
        score = _score(paren.group(1))
        name = bold[:paren.start()] + bold[paren.end():]
    name = re.sub(r"^[\s–—-]+|[\s–—-]+$", "", name)
    # description = everything in <p> after the <strong>
    desc_parts, seen = [], False
    for child in p.children:
        if child is strong:
            seen = True
            continue
        if seen:
            desc_parts.append(child if isinstance(child, str) else child.get_text())
    desc = re.sub(r"\s+", " ", "".join(desc_parts)).strip()
    desc = desc.lstrip("–-").strip() or None
    return {
        "player_name": name,
        "player_id": _player_id(a),
        "score": score,
        "is_mvp": False,
        "description": desc,
    }


# Leading "O melhor em campo" / "A figura" -- those <p>s are handled by the MVP
# box pass, not as rating rows.
MVP_LEAD_RE = re.compile(r"^\s*(o melhor em campo|a figura)\b", re.I)


def _immediate_rating(a) -> tuple[bool, int | None]:
    """(has_rating_token, score) for the bare '(..)' right after a player link:
    '(6)' -> (True, 6), '(-)' -> (True, None), prose like '(35 anos)' or a plain
    mention -> (False, None). The token -- not a numeric score -- is what marks a
    ratings entry, so unrated subs written '(-)' are still counted."""
    nxt = a.next_sibling
    s = nxt if isinstance(nxt, str) else (nxt.get_text() if nxt else "")
    m = re.match(r"\s*\(\s*([\d\-–]+)\s*\)", s or "")
    return (True, _score(m.group(1))) if m else (False, None)


def _deepdive_player(p, a) -> dict:
    """Header-less deep dive: '<a>Name</a> (5) — narrative...' (one per <p>)."""
    full = p.get_text(" ", strip=True)
    parts = re.split(r"\s[–—-]\s", full, maxsplit=1)
    return {
        "player_name": a.get_text(strip=True),
        "player_id": _player_id(a),
        "score": _immediate_rating(a)[1],
        "is_mvp": False,
        "description": (parts[1].strip() if len(parts) > 1 else None) or None,
    }


def _headerless_players(p, current: str) -> list[dict]:
    """Players from a <p> with no notas <strong> header. Two big-three shapes:
      * inline ratings list (opponent page): 'A (6); B (4), C (4) ...' -- most
        links carry an immediate score  -> one entry per link.
      * per-player deep dive (big-three page): '<a>Trubin</a> (5) — text', where
        the rated player is the FIRST link and the rest are narrative mentions.
    Returns [] for ordinary prose (no link carries an immediate rating)."""
    if MVP_LEAD_RE.search(p.get_text(" ", strip=True)):
        return []  # MVP box -> handled by _mvp_boxes
    links = p.find_all("a", attrs={"data-resource-type": "player"})
    if not links:
        return []
    rated = [_immediate_rating(a) for a in links]
    n_rated = sum(has for has, _ in rated)
    if n_rated >= 2 and n_rated >= len(links) * 0.6:
        return [{
            "player_name": a.get_text(strip=True), "player_id": _player_id(a),
            "score": sc, "is_mvp": False, "description": None,
        } for a, (_, sc) in zip(links, rated)]
    # Deep dive: the rated player is the first link carrying a rating token --
    # not necessarily links[0], which is sometimes an empty/icon anchor.
    for a, (has, _) in zip(links, rated):
        if has:
            return [_deepdive_player(p, a)]
    return []


def parse_page(html: str, default_team: str | None = None) -> dict:
    """Return {teams: {team_name: [players]}, has_melhor, mvp_names: {...}}."""
    soup = BeautifulSoup(html, "lxml")
    teams: dict[str, list] = {}
    current = default_team
    if current:
        teams.setdefault(current, [])

    # Crónica/inline pages carry "as notas d<team>" inside a <p><strong> header;
    # big-three "as notas" pages put that header in an <h1>/<h2> and the ratings
    # in header-less <p>s. Detect which, so header-less parsing only runs where
    # there's no <p><strong> header to misread.
    has_strong_header = any(
        (s := p.find("strong")) and NOTAS_RE.search(s.get_text(" ", strip=True))
        for p in soup.find_all("p"))

    for p in soup.find_all("p"):
        strong = p.find("strong")
        if strong and NOTAS_RE.search(strong.get_text(" ", strip=True)):
            current = _team_of_header(strong) or current
            if current:
                teams.setdefault(current, [])
                inline = _layout_a_players(p, strong)
                if inline:
                    teams[current].extend(inline)
            continue
        if strong and strong.find("a", attrs={"data-resource-type": "player"}):
            player = _layout_b_player(p, strong)
            if player and current:
                teams.setdefault(current, [])
                teams[current].append(player)
            continue
        if not has_strong_header and current:
            extra = _headerless_players(p, current)
            if extra:
                teams.setdefault(current, [])
                teams[current].extend(extra)

    # MVP boxes. "A figura" counts as MVP only when there's no "O melhor em
    # campo" on the page; "O melhor em campo" always wins.
    boxes = _mvp_boxes(soup)
    has_melhor = any(b["kind"] == "melhor" for b in boxes)

    def _target_team(team_hint):
        if team_hint:
            for tn in teams:
                if _norm(tn) == _norm(team_hint) or _norm(team_hint) in _norm(tn) \
                        or _norm(tn) in _norm(team_hint):
                    return tn
        if default_team and default_team in teams:
            return default_team
        if len(teams) == 1:
            return next(iter(teams))
        return None

    for b in boxes:
        is_mvp = b["kind"] == "melhor" or (b["kind"] == "figura" and not has_melhor)
        found = False
        for plist in teams.values():
            for pl in plist:
                if _norm(pl["player_name"]) == _norm(b["name"]):
                    pl["is_mvp"] = pl["is_mvp"] or is_mvp
                    if not pl.get("description") and b["description"]:
                        pl["description"] = b["description"]
                    found = True
        if not found:
            target = _target_team(b["team"])
            if target:
                teams[target].append({
                    "player_name": b["name"], "player_id": None,
                    "score": b["score"], "is_mvp": is_mvp,
                    "description": b["description"],
                })

    return {"teams": teams, "has_melhor": has_melhor, "mvp_boxes": boxes}


def _pick_team(teams: dict, want: str) -> list:
    """Best-effort match of a parsed team bucket to the wanted team name.
    A Bola's name often differs from zerozero's ('Vitória SC' vs 'Vitória de
    Guimarães', 'Sporting' vs 'Sporting CP'), so after exact/substring checks
    fall back to distinctive-token overlap and take the best-matching bucket."""
    wn = _norm(want)
    for name, players in teams.items():
        if _norm(name) == wn:
            return players
    for name, players in teams.items():
        if wn and (wn in _norm(name) or _norm(name) in wn):
            return players
    want_tokens = _team_tokens(want)
    best, best_score = [], 0
    for name, players in teams.items():
        nn = _norm(name)
        score = sum(1 for t in want_tokens if t in nn)
        if score > best_score:
            best, best_score = players, score
    return best


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------


def scrape_match(match: dict, *, single_url: str | None = None,
                 home_url: str | None = None, away_url: str | None = None,
                 delay: float = 1.0) -> dict:
    """Scrape one match's A Bola ratings into the merged output structure.

    URLs may be injected (skips Google search) for testing/resilience.
    """
    home, away = match["home_team"], match["away_team"]
    gdate = _parse_date(match["game_date"])
    big3 = bool(match.get("is_big_three_match"))

    out = {
        "match": f"{home} vs {away}",
        "date": match["game_date"],
        "format_detected": 2 if big3 else 1,
        "urls_used": [],
        "home_team_ratings": [],
        "away_team_ratings": [],
    }

    if big3:
        if home_url is None and single_url is None:
            home_url = find_team_notas(home, gdate)
            time.sleep(delay)
        if away_url is None and single_url is None:
            away_url = find_team_notas(away, gdate)
        if home_url:
            out["urls_used"].append(home_url)
            page = parse_page(fetch(home_url), default_team=home)
            out["home_team_ratings"] = _pick_team(page["teams"], home) or \
                next(iter(page["teams"].values()), [])
            time.sleep(delay)
        if away_url:
            out["urls_used"].append(away_url)
            page = parse_page(fetch(away_url), default_team=away)
            out["away_team_ratings"] = _pick_team(page["teams"], away) or \
                next(iter(page["teams"].values()), [])
    else:
        if single_url is None:
            single_url = find_cronica(home, away, gdate)
        if single_url:
            out["urls_used"].append(single_url)
            page = parse_page(fetch(single_url))
            out["home_team_ratings"] = _pick_team(page["teams"], home)
            out["away_team_ratings"] = _pick_team(page["teams"], away)

    return out


def scrape_matches(matches: list[dict], *, delay: float = 1.0) -> list[dict]:
    results = []
    for i, m in enumerate(matches, 1):
        print(f"[{i}/{len(matches)}] {m['home_team']} vs {m['away_team']}",
              file=sys.stderr)
        try:
            results.append(scrape_match(m, delay=delay))
        except Exception as err:  # noqa: BLE001
            print(f"  ! failed: {err}", file=sys.stderr)
            results.append({"match": f"{m['home_team']} vs {m['away_team']}",
                            "date": m.get("game_date"), "error": str(err)})
        time.sleep(delay)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", help="JSON file: a list of match dicts")
    ap.add_argument("--out", help="Write results JSON here (else stdout)")
    ap.add_argument("--delay", type=float, default=1.5)
    args = ap.parse_args()

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            matches = json.load(fh)
    else:
        matches = json.load(sys.stdin)

    results = scrape_matches(matches, delay=args.delay)
    text = json.dumps(results, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.out} ({len(results)} matches).", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
