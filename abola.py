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
# Search backend (pluggable)
# ----------------------------------------------------------------------------


def _ddg_search(query: str, num_results: int = 15, *, retries: int = 3,
                backoff: float = 6.0) -> list[str]:
    """DuckDuckGo backend (no API key). Retries with backoff because DDG
    throttles bursts of queries (space your runs out)."""
    from ddgs import DDGS
    for attempt in range(1, retries + 1):
        try:
            with DDGS() as d:
                urls = [r["href"] for r in d.text(query, max_results=num_results)
                        if r.get("href")]
            if urls:
                return urls
        except Exception as err:  # noqa: BLE001
            print(f"  ! DDG search {attempt}/{retries} {query!r}: {err}",
                  file=sys.stderr)
        if attempt < retries:
            time.sleep(backoff * attempt)
    return []


def _google_search(query: str, num_results: int = 15) -> list[str]:
    """Fallback backend (googlesearch-python; often rate-limited/blocked)."""
    from googlesearch import search
    try:
        return list(search(query, num_results=num_results, lang="pt"))
    except Exception as err:  # noqa: BLE001
        print(f"  ! Google search failed for {query!r}: {err}", file=sys.stderr)
        return []


# Default to DuckDuckGo. Override with your own f(query, num_results) -> [url]
# (e.g. a SerpAPI / Google Custom Search backend) if you prefer.
SEARCH = _ddg_search


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


def find_article(query: str, game_date: date, *, require: list[str] | None = None,
                 num_results: int = 30) -> str | None:
    """Search, keep abola.pt article results whose (URL) date is in
    [game_date, game_date + MAX_DATE_LAG_DAYS], closest first. Prefer URLs that
    contain ALL `require` substrings (e.g. ['notas','sporting']); fall back to
    the closest dated article if none match."""
    require = [r.lower() for r in (require or [])]
    dated = []
    for url in SEARCH(query, num_results):
        if "abola.pt" not in url or url.rstrip("/").endswith("comments"):
            continue
        d = url_date(url)
        if d is None:
            continue
        lag = (d - game_date).days
        if 0 <= lag <= MAX_DATE_LAG_DAYS:
            dated.append((lag, url))
    dated.sort()
    if require:
        # Hard filter: only return an article that really is the notas/crónica
        # page (never fall back to an unrelated same-day article).
        strict = [u for _, u in dated if all(r in u.lower() for r in require)]
        return strict[0] if strict else None
    return dated[0][1] if dated else None


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
    m = re.search(r"as notas d\w*\s+(?:jogadores\s+d\w*\s+)?(.+?)\s*:",
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
    sm = re.match(r"\s*(-?\d+)\b", bold)
    score = int(sm.group(1)) if sm else None
    # name = bold minus leading score and trailing dash
    name = bold
    if sm:
        name = name[sm.end():]
    # strip leading/trailing dashes (hyphen, en-dash, em-dash) and spaces
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


def parse_page(html: str, default_team: str | None = None) -> dict:
    """Return {teams: {team_name: [players]}, has_melhor, mvp_names: {...}}."""
    soup = BeautifulSoup(html, "lxml")
    teams: dict[str, list] = {}
    current = default_team
    if current:
        teams.setdefault(current, [])

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
    """Best-effort match of a parsed team bucket to the wanted team name."""
    wn = _norm(want)
    for name, players in teams.items():
        if _norm(name) == wn:
            return players
    for name, players in teams.items():
        if wn and (wn in _norm(name) or _norm(name) in wn):
            return players
    return []


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
        # Include the opponent so the search pins the *specific* match's notes.
        if home_url is None and single_url is None:
            home_url = find_article(f'site:abola.pt "as notas do {home}" {away}',
                                    gdate, require=["notas", _slug(home)])
            time.sleep(delay)  # space out searches (DDG rate-limits bursts)
        if away_url is None and single_url is None:
            away_url = find_article(f'site:abola.pt "as notas do {away}" {home}',
                                    gdate, require=["notas", _slug(away)])
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
            single_url = find_article(f'site:abola.pt "{home} {away} crónica"',
                                      gdate, require=["cronica"])
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
