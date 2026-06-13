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

# Allow same-day or the next couple of days for the article to appear. Kept
# tight (2) on purpose: a club's NEXT game is at least ~3 days off (a midweek
# cup/Europe game plus the weekend league game), so a wider window would let a
# later game's "as notas do <team>" page -- which names only that one team, so
# its date is the only thing tying it to a match -- be mistaken for this game's.
MAX_DATE_LAG_DAYS = 2


# ----------------------------------------------------------------------------
# Search backend — A Bola's own search at /pesquisar (server-rendered results)
# ----------------------------------------------------------------------------

SEARCH_URL = "https://www.abola.pt/pesquisar?q="
ARTICLE_RE = re.compile(
    r'href="((?:https://www\.abola\.pt)?/(?:futebol/)?noticias/[a-z0-9\-]+-\d{12,})"')

# Search results are title-only and newest-first, so a match's ratings page sinks
# deeper as later games spawn their own "as notas do X" pages. Backfilling an old
# match therefore needs to page well past the first results; the until_date walk
# stops the moment a page predates the match, so this cap is only ever hit when
# the window genuinely can't be reached. Generous so a months-old game is found.
MAX_SEARCH_PAGES = 50


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
    """'(5)' -> 5 ; any dash variant '(-)'/'(–)'/'—'/'-' -> 0 (reporter gave no rating) ; '' / None -> None (not scraped)."""
    if raw is None or raw.strip() == "":
        return None
    m = re.search(r"\d+", raw)
    if m:
        return int(m.group(0))
    # No digit → reporter gave no rating (-, –, —, with or without parens)
    return 0


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
CLUB_ABBR = {"fc", "sc", "ac", "cd", "sl", "ad", "gd", "cf", "sad",
             "ud", "cs", "sg", "praia"}

# Interchangeable names for the same club -- zerozero and A Bola don't always
# agree, and some are renames that share no letters (AVS = the old Desportivo
# das Aves, written "AFS"/"AVS"/"Aves SAD"). Each set is one club; any token in
# a set matches the whole set. Add groups here as new mismatches surface.
TEAM_ALIASES = [
    {"avs", "afs", "aves"},
    # A Bola names some clubs by their CITY, not the zerozero "SC/FC" form:
    # 'Vitória SC' is written 'V. Guimarães' / 'Guimarães' (shares no token).
    {"vitoria", "guimaraes"},
]

# Per-team alternative NAMES A Bola may use, keyed by _norm(zerozero name) ->
# [alias display names]. Empty by default; reporter_sync populates it from the
# DB (the team-aliases page) so e.g. 'AFS' -> ['Aves SAD'] without code changes.
TEAM_ALIAS_NAMES: dict[str, list[str]] = {}


def _alias_names(team: str) -> list[str]:
    """A team's own name plus any configured alias names (for search queries)."""
    return [team, *TEAM_ALIAS_NAMES.get(_norm(team), [])]


def _expand_aliases(tokens: set[str]) -> set[str]:
    out = set(tokens)
    for t in tokens:
        for grp in TEAM_ALIASES:
            if t in grp:
                out |= grp
    return out


def _team_tokens(team: str) -> set[str]:
    """Distinctive name tokens for matching a team in a URL / page. A Bola uses
    short forms ('Estoril Praia' -> 'estoril', 'Vitória SC' -> 'vitoria',
    'SC Braga' -> 'braga'), so drop club-type words and keep the rest, then add
    known aliases (built-in groups + configured alias names) so e.g. 'AFS' still
    matches A Bola's 'Aves SAD'."""
    toks = {_norm(w) for nm in _alias_names(team)
            for w in re.split(r"\s+", nm.strip()) if w}
    sig = {t for t in toks if t and len(t) >= 3 and t not in CLUB_ABBR}
    return _expand_aliases(sig or {_norm(team)})


def _clean_team(team: str) -> str:
    """Team name without club-type words, matching A Bola's short form
    ('Estoril Praia' -> 'Estoril', 'Vitória SC' -> 'Vitória')."""
    words = [w for w in re.split(r"\s+", team.strip())
             if _norm(w) not in CLUB_ABBR]
    return " ".join(words) or team


def _in_window(d: date, game_date: date) -> bool:
    return 0 <= (d - game_date).days <= MAX_DATE_LAG_DAYS


# Fallback discovery via a team's own A Bola news feed. /pesquisar is title-only,
# so a ratings page with a creative title that names neither the team nor "notas"
# (common for a big-three opponent, e.g. "Salão de Barbero abriu cedo...") is
# unreachable by search. But A Bola TAGS every article to its teams, and a team's
# page lists them newest-first at /futebol/<slug>-<id>?page=N, so we can page back
# to the match window and confirm by opening. Feed density varies by club (~6-12
# days/page), so reaching a season's start can take ~55 pages; cap generously
# since the walk stops the moment it reaches the window. A ratings page is
# unmistakable (it parses to a whole XI).
MAX_FEED_PAGES = 60
FEED_PAGE_DELAY = 0.3
MIN_FEED_RATINGS = 8
# A Bola team-page URLs come in two shapes -- "/futebol/<slug>-<id>" and the
# slug-less "/futebol/<id>" -- so the slug is optional. Anchored at the end so a
# news URL ("/futebol/competicao/...") never matches.
_TEAM_PAGE_RE = re.compile(r"^/futebol/(?:[a-z0-9-]+-)?\d+$")
# Youth / women's / B sides share a club's tokens ("Gil Vicente Sub-23"); their
# slugs carry a marker, so exclude them -- we want the senior team's feed.
_TEAM_VARIANT_RE = re.compile(
    r"sub-?\d|\bfeminin|\bjunior|juvenil|\bsub23\b|-b-\d", re.I)


def _abs(href: str) -> str:
    """Normalise an A Bola href to a https://www.abola.pt absolute URL (some
    autolinks use bare 'http://abola.pt' without the www)."""
    if href.startswith("/"):
        return "https://www.abola.pt" + href
    return re.sub(r"^https?://(?:www\.)?abola\.pt", "https://www.abola.pt", href)


def _search_terms(team: str) -> list[str]:
    """Query strings to look a team up by, most specific first: its own/alias
    names, their club-type-stripped short forms ('Casa Pia AC' -> 'Casa Pia',
    which A Bola indexes when the full name returns nothing), then distinctive
    tokens ('amadora' finds 'Estrela da Amadora' when 'Est. Amadora' doesn't)."""
    terms, seen = [], set()
    for nm in _alias_names(team):
        for t in (nm, _clean_team(nm)):
            t = t.strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                terms.append(t)
    for tok in _team_tokens(team):
        if tok not in seen:
            seen.add(tok)
            terms.append(tok)
    return terms


def _team_page_score(text: str, team: str) -> int:
    """How well a team autolink's text matches `team`: 3 exact, 2 cleaned-name
    equal, 1 token overlap, 0 none. Lets the senior side win over a youth side
    that merely shares tokens."""
    nt, na = _norm(text), _norm(team)
    if nt == na:
        return 3
    if nt == _norm(_clean_team(team)):
        return 2
    return 1 if _team_tokens(text) & _team_tokens(team) else 0


def find_team_page(team: str) -> str | None:
    """A team's A Bola page URL, read from a team autolink on a recent article
    tagged with it. Opens several search hits and keeps the best-matching senior
    team link (exact name beats token overlap; youth/women's sides excluded), so
    'Gil Vicente' resolves to the senior page, not 'Gil Vicente Sub-23'. Cached."""
    if team in _TEAM_PAGE_CACHE:
        return _TEAM_PAGE_CACHE[team]
    best, best_score = None, 0
    for nm in _search_terms(team):
        for _d, u in abola_search(nm, pages=2):
            try:
                soup = BeautifulSoup(fetch(u), "lxml")
            except Exception:  # noqa: BLE001
                continue
            for a in soup.find_all("a", attrs={"data-resource-type": "team"}):
                path = re.sub(r"^https?://(?:www\.)?abola\.pt", "",
                              a.get("href", "")).split("?")[0]
                if not _TEAM_PAGE_RE.match(path) or _TEAM_VARIANT_RE.search(path):
                    continue
                score = _team_page_score(a.get_text(strip=True), team)
                if score > best_score:
                    best, best_score = _abs(a["href"]), score
            if best_score >= 3:           # exact name -- can't do better
                break
        if best_score >= 3:
            break
    _TEAM_PAGE_CACHE[team] = best
    return best


_TEAM_PAGE_CACHE: dict[str, str | None] = {}


def _feed_articles(team_page: str, page: int) -> list[tuple[date, str]]:
    u = team_page + (f"?page={page}" if page > 1 else "")
    soup = BeautifulSoup(fetch(u), "lxml")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        if ARTICLE_RE.search(f'href="{a["href"]}"'):
            h = _abs(a["href"])
            d = url_date(h)
            if d and h not in seen:
                seen.add(h)
                out.append((d, h))
    return out


def walk_team_feed(team: str, game_date: date):
    """Yield candidate article URLs in the match window from a team's news feed,
    paging back (newest-first) until the page reaches past the match date."""
    team_page = find_team_page(team)
    if not team_page:
        return
    yielded = set()
    for page in range(1, MAX_FEED_PAGES + 1):
        if page > 1:
            time.sleep(FEED_PAGE_DELAY)
        try:
            arts = _feed_articles(team_page, page)
        except Exception:  # noqa: BLE001
            break
        if not arts:
            break
        for d, u in sorted(arts):
            if _in_window(d, game_date) and u not in yielded:
                yielded.add(u)
                yield u
        # A recurring "latest" widget keeps a few fresh links on every page, so
        # judge depth by the OLDEST real article: once it predates the match, the
        # window has been fully covered on this and earlier pages.
        if min(d for d, _ in arts) < game_date:
            break


def _has_team_ratings(url: str, team: str, *, default_team: str | None = None) -> bool:
    """True if `url` parses to a full ratings list for `team` (>= a near-XI). The
    verification that separates a real notas/destaques page from a passing mention
    -- and that stops a generic token (e.g. 'casa' in '...chaves-de-casa...') from
    accepting an unrelated team's page."""
    try:
        page = parse_page(fetch(url), default_team=default_team)
    except Exception:  # noqa: BLE001
        return False
    return len(_pick_team(page["teams"], team)) >= MIN_FEED_RATINGS


def _team_ratings_page(team: str, game_date: date) -> str | None:
    """Open in-window feed candidates and return the first that parses to a full
    team-ratings list for `team` (a real notas/destaques page, not a mention)."""
    for u in walk_team_feed(team, game_date):
        if _has_team_ratings(u, team, default_team=team):
            return u
    return None


def find_team_notas(team: str, game_date: date) -> str | None:
    """Find the 'as notas do <team>' page for a match on/just after game_date.
    A Bola's name for a team may differ from zerozero's ('AFS' -> 'Aves SAD'),
    so we SEARCH with the team's aliases too -- not just filter results by them
    (the bug that hid AFS: the query asked for 'AFS', which A Bola doesn't index,
    while the page is 'as notas do Aves SAD').

    A single-team notas page names only `team`, so its DATE is all that ties it to
    a match -- which is why MAX_DATE_LAG_DAYS is kept tight (2): a wider window
    could reach the team's NEXT game and return that game's notas instead."""
    tokens = _team_tokens(team)
    for term in _search_terms(team):
        # A Bola titles the ratings page "as notas do X" or "os destaques do X".
        for q in (f"as notas do {term}", f"notas {term}",
                  f"os destaques do {term}", f"destaques {term}"):
            # Page back (newest-first) until results predate the match, so an old
            # game's ratings page is reached and not just the first ~2 pages of
            # recent ones -- the until_date walk stops as soon as the window is
            # covered, so this rarely fetches anywhere near MAX_SEARCH_PAGES.
            cands = [(d, u) for d, u in abola_search(
                         q, pages=MAX_SEARCH_PAGES, until_date=game_date)
                     if _in_window(d, game_date)
                     and ("notas" in u or "destaques" in u)
                     and any(t in u.lower() for t in tokens)]
            cands.sort()
            # Open-verify: a URL token can hit by accident ('casa' in
            # '...chaves-de-casa...as-notas-do-benfica'), so confirm the page
            # really carries this team's ratings before trusting it.
            for _d, u in cands:
                if _has_team_ratings(u, team):
                    return u
    # Fallback: a creatively-titled ratings page (search can't see it) -- page the
    # team's own A Bola feed back to the match window and open-verify.
    return _team_ratings_page(team, game_date)


def find_cronica(home: str, away: str, game_date: date) -> str | None:
    """Find the single 'crónica' page (non-big-three). A Bola's /pesquisar is
    TITLE-only (body text isn't indexed) and result cards carry no 'crónica'
    marker, so there's no content signal to query/filter by. We therefore cast a
    wide net from the two title-based signals A Bola exposes and CONFIRM by
    opening:
      * the word 'crónica' (catches crónica-titled reports), and
      * the team names (many reports title the teams even without 'crónica',
        e.g. 'Concerto europeu de SC Braga e Famalicão...').
    Candidates in the date window are opened and accepted only if the parsed
    page carries both teams' ratings -- which filters out title false hits like
    'Nacional' (= national)."""
    ht, at = _team_tokens(home), _team_tokens(away)
    ch, ca = _clean_team(home), _clean_team(away)
    cands: dict[str, date] = {}
    for d, u in abola_search("crónica", pages=MAX_SEARCH_PAGES, until_date=game_date):
        if "cronica" in u and _in_window(d, game_date):
            cands[u] = d
    for q in (f"{ch} {ca}", f"{ca} {ch}"):
        for d, u in abola_search(q, pages=2):
            if _in_window(d, game_date):
                cands[u] = d
    for u in sorted(cands, key=lambda x: cands[x]):
        try:
            page = parse_page(fetch(u))
        except Exception:  # noqa: BLE001
            continue
        names = " ".join(_norm(t) for t in page["teams"])
        if any(t in names for t in ht) and any(t in names for t in at):
            return u
    # Fallback: title names neither team -> page the home team's A Bola feed back
    # to the match window and accept a page carrying BOTH teams' ratings.
    for u in walk_team_feed(home, game_date):
        if u in cands:
            continue
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

# Header phrase. A Bola writes it as "As notas dos jogadores do X", "NOTAS DOS
# JOGADORES DO X" (no leading "As"), or "os destaques do X" -- so "as"/"os" are
# optional and "notas"/"destaques" both count. Crónicas instead head each team's
# inline ratings with the starting eleven, "Onze do X (4x3x3): Name (6); ...",
# so "(o) onze" counts as a header too.
_HDR = r"(?:(?:as\s+)?notas|(?:os\s+)?destaques|(?:o\s+)?onze)"
NOTAS_RE = re.compile(_HDR + r"\s+d", re.I)
# A bare rating token. A Bola rates 0-10, so cap it there: this rejects not just
# formations "(4x2x3x1)" / "(35 anos)" but stray stats in prose like "(43)" that
# would otherwise look like a rating and pull narrative into the list. An unrated
# player is "(-)" / "(–)" / "(—)" (hyphen, en-dash or em-dash) -> score None.
_RATING = r"(?:10|\d|[-–—])"
# Closing of a rating token. Normally ")", but A Bola sometimes typos it as ";"
# or "," ("Ricardo Horta (5;") -- accept those so the player isn't dropped.
_CLOSE = r"[);,]"
TOKEN_RE = re.compile(r"\(\s*" + _RATING + r"\s*" + _CLOSE)
# One inline ratings entry: a capitalised name immediately followed by "(score)".
# A name never contains ":", so excluding it drops in-list labels like
# "Suplentes :" that would otherwise glue onto the first substitute's name.
ENTRY_RE = re.compile(r"([A-ZÀ-Ÿ][^():]*?)\s*\(\s*(" + _RATING + r")\s*" + _CLOSE)
# MVP box. The parenthesis holds EITHER a score ("Richard Ríos (7)") OR the team
# ("Larrazabal (Casa Pia)") -- both forms appear, so capture it raw and classify.
MVP_RE = re.compile(
    r"((?:o\s+)?melhor em campo|a figura)(?:\s+d[oae]\s+([^:<\n]+?))?\s*:\s*"
    r"([^()<\n]+?)\s*\(\s*([^)<\n]+?)\s*\)", re.I)
# Suffix form: "Prestianni (7) — o melhor em campo" (name + score, label after).
MVP_SUFFIX_RE = re.compile(
    r"([^()<\n]+?)\s*\(\s*(" + _RATING + r")\s*\)\s*[—–-]\s*"
    r"((?:o\s+)?melhor em campo|a figura)", re.I)
# Leading "O melhor em campo" / "A figura" -- handled by the MVP box pass, never
# as a rating row.
MVP_LEAD_RE = re.compile(r"^\s*(o melhor em campo|a figura)\b", re.I)


def _player_id(a) -> str | None:
    if a.has_attr("data-resource-id"):
        return a["data-resource-id"]
    m = re.search(r"/jogador/[^/]*?-(\d+)\b", a.get("href", ""))
    return m.group(1) if m else None


def _header_strong(el):
    """The <strong> holding the 'as notas d…' phrase, if the header uses one."""
    s = el.find("strong")
    return s if s and NOTAS_RE.search(s.get_text(" ", strip=True)) else None


def _team_of_header(el) -> str | None:
    """Team named in an 'as notas d<team>' header (h1/h2/p/strong)."""
    tl = el.find("a", attrs={"data-resource-type": "team"})
    if tl and NOTAS_RE.search(el.get_text(" ", strip=True)):
        return tl.get_text(strip=True)
    # Read the name from the header text. When a <strong> holds the phrase, read
    # only IT -- otherwise, with no colon ("NOTAS DO AVES SAD Adriel Ramos (8)"),
    # we'd swallow the first player into the team name. The ":" / "(formation)"
    # suffix often live outside the bold, and on big-three pages the phrase is in
    # the headline ("...(as notas do Benfica)"), so stop at "(", ":" or end.
    src = _header_strong(el) or el
    m = re.search(_HDR + r"\s+d\w*\s+(?:jogadores\s+d\w*\s+)?(.+?)\s*(?:\(|:|$)",
                  src.get_text(" ", strip=True), re.I)
    return re.sub(r"^[(\s]+|[)\s]+$", "", m.group(1)) or None if m else None


def _after_header(el) -> str:
    """The inline ratings text that follows the header on the same line. When a
    <strong> holds ONLY the phrase, the ratings are its following siblings (robust
    even with no colon). But some pages bold the WHOLE line ('Notas do Alverca:
    Matheus Mendes (5); ...' all in one <strong>), so nothing follows it -- there,
    fall back to stripping the 'as notas d…:' prefix off the full text."""
    strong = _header_strong(el)
    if strong:
        parts, seen = [], False
        for ch in el.children:
            if ch is strong:
                seen = True
            elif seen:
                parts.append(ch if isinstance(ch, str) else ch.get_text())
        after = re.sub(r"\s+", " ", "".join(parts)).strip()
        if TOKEN_RE.search(after):  # ratings really are after the bold
            return after
    return _strip_header_prefix(el.get_text(" ", strip=True))


def _is_notas_header(el) -> bool:
    """True if `el` introduces a team's ratings. Ratings rows ('Name (6); ...')
    never contain 'as notas', so the phrase reliably marks a header -- but only
    in a heading, a <strong>, or at the very start of a paragraph (not buried in
    prose)."""
    text = el.get_text(" ", strip=True)
    if not NOTAS_RE.search(text):
        return False
    if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return True
    if re.match(r"\s*" + _HDR + r"\b", text, re.I):
        return True
    s = el.find("strong")
    return bool(s and NOTAS_RE.search(s.get_text(" ", strip=True)))


def _strip_header_prefix(text: str) -> str:
    """Drop a leading 'As notas d<team> (formation) :' so only the ratings list
    that may follow it in the same element remains."""
    return re.sub(r"(?i)^.*?" + _HDR + r"\s+d.*?:\s*", "", text, count=1)


def _link_id_map(el) -> dict:
    """{normalised player name -> id} for every player autolink in `el`."""
    out = {}
    for a in el.find_all("a", attrs={"data-resource-type": "player"}):
        key = _norm(a.get_text(strip=True))
        if key:
            out.setdefault(key, _player_id(a))
    return out


def _attach_id(name: str, id_map: dict) -> str | None:
    """Map a parsed name to an autolink id: exact, else longest name that is a
    substring (handles a player linked across two anchors, e.g. 'Miguel Maga')."""
    n = _norm(name)
    if n in id_map:
        return id_map[n]
    best, best_len = None, 0
    for k, v in id_map.items():
        if k and (k in n or n in k) and len(k) > best_len:
            best, best_len = v, len(k)
    return best


def _clean_name(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" –—-")


def _player_name(s: str) -> str:
    """Cleaned name, or '' if it doesn't look like one (too long => a sentence
    captured by mistake). Player names are short; narratives are not."""
    name = _clean_name(s)
    return name if 0 < len(name) <= 35 else ""


def _inline_entries(text: str, id_map: dict) -> list[dict]:
    """Parse 'Name (6); Name (4), ...' from TEXT (so players A Bola didn't
    autolink are still captured), attaching ids where a link exists."""
    out = []
    for m in ENTRY_RE.finditer(text):
        name = _player_name(m.group(1))
        if name:
            out.append({
                "player_name": name, "player_id": _attach_id(name, id_map),
                "score": _score(m.group(2)), "is_mvp": False, "description": None,
            })
    return out


def _immediate_rating(a) -> tuple[bool, int | None]:
    """(has_rating_token, score) for the bare '(..)' right after a player link:
    '(6)' -> (True, 6), '(-)' -> (True, None), prose like '(35 anos)' or a plain
    mention -> (False, None)."""
    nxt = a.next_sibling
    s = nxt if isinstance(nxt, str) else (nxt.get_text() if nxt else "")
    m = re.match(r"\s*\(\s*(" + _RATING + r")\s*" + _CLOSE, s or "")
    return (True, _score(m.group(1))) if m else (False, None)


def _deepdive_entry(el, id_map: dict) -> list[dict]:
    """One deep-dive rating row. The score sits in any of three spots:
        'Diogo Costa (6) — …'   (after the name)
        '(1) Bednarek — …'      (parenthetical, before the name)
    plus the leading bare-number form handled by _leading_score_row. Prefer an
    autolink for the id; fall back to text when the player isn't linked."""
    full = el.get_text(" ", strip=True)
    parts = re.split(r"\s[–—-]\s", full, maxsplit=1)
    desc = (parts[1].strip() or None) if len(parts) > 1 else None
    a = el.find("a", attrs={"data-resource-type": "player"})
    # Parenthetical score BEFORE the name: "(6) William Gomes — …"
    lead = re.match(r"\s*\(\s*(" + _RATING + r")\s*\)\s*(.+)", parts[0])
    if lead:
        name = _player_name(lead.group(2))
        if name:
            return [{
                "player_name": name,
                "player_id": _player_id(a) if a else _attach_id(name, id_map),
                "score": _score(lead.group(1)), "is_mvp": False, "description": desc,
            }]
    # Score AFTER the name: prefer an autolink that carries the rating.
    for link in el.find_all("a", attrs={"data-resource-type": "player"}):
        has, sc = _immediate_rating(link)
        if has:
            return [{
                "player_name": link.get_text(strip=True), "player_id": _player_id(link),
                "score": sc, "is_mvp": False, "description": desc,
            }]
    m = ENTRY_RE.search(full)
    if not m:
        return []
    name = _player_name(m.group(1))
    return [{
        "player_name": name, "player_id": _attach_id(name, id_map),
        "score": _score(m.group(2)), "is_mvp": False, "description": desc,
    }] if name else []


def _leading_score_row(el) -> list[dict]:
    """Deep dive with the score BEFORE the name. Three spellings seen:
        '7 Rui Silva — narrative'        (score then name, space-separated)
        '5 Diomande — narrative'         (bold is just the score; name is a link)
        '5 - MAXI ARAÚJO — narrative'    ('-' between the score and the name)
    Parse from TEXT: the score is the leading number, the name is what's left
    after dropping the score (and a '-' separator) up to the narrative dash, and
    the id comes from the first player link. A player autolink is required so
    listicles ('5 Coisas — …') are out."""
    full = el.get_text(" ", strip=True)
    m = re.match(r"\s*(10|\d)(?!\d)", full)
    if not m:
        return []
    a = el.find("a", attrs={"data-resource-type": "player"})
    if not a:
        return []
    rest = re.sub(r"^\s*\d+\s*[–—-]*\s*", "", full)   # drop score + '-' separator
    parts = re.split(r"\s[–—-]\s", rest, maxsplit=1)   # split off the narrative
    name = _player_name(parts[0])
    if not name or not name[:1].isupper():
        return []
    return [{
        "player_name": name, "player_id": _player_id(a),
        "score": int(m.group(1)), "is_mvp": False,
        "description": (parts[1].strip() or None) if len(parts) > 1 else None,
    }]


def _rating_rows(el) -> list[dict]:
    """Ratings from a NON-header block. Many '(score)' tokens -> an inline list;
    one -> a parenthetical deep-dive paragraph; none -> maybe a leading-score
    deep dive ('7 Rui Silva — …'), else prose (ignored)."""
    text = el.get_text(" ", strip=True)
    if not text or MVP_LEAD_RE.search(text):
        return []
    n = len(TOKEN_RE.findall(text))
    if n == 0:
        return _leading_score_row(el)
    id_map = _link_id_map(el)
    return _inline_entries(text, id_map) if n >= 2 else _deepdive_entry(el, id_map)


def _score_name_from_text(text: str) -> tuple:
    """(name, score, desc) from a player line in any rating spelling:
    'score - Name — desc', 'score Name — desc', '(score) Name', 'Name (score)'."""
    text = text.strip()
    m = re.match(r"(10|\d)(?!\d)\s*[–—-]*\s*(.+)", text)           # leading score
    if m:
        rest = re.split(r"\s[–—-]\s", m.group(2), maxsplit=1)
        name = _player_name(rest[0])
        if name and name[:1].isupper():
            return name, int(m.group(1)), (rest[1].strip() or None) if len(rest) > 1 else None
    m = re.match(r"\(\s*(" + _RATING + r")\s*\)\s*(.+)", text)      # (score) Name
    if m:
        rest = re.split(r"\s[–—-]\s", m.group(2), maxsplit=1)
        name = _player_name(rest[0])
        if name:
            return name, _score(m.group(1)), (rest[1].strip() or None) if len(rest) > 1 else None
    m = ENTRY_RE.search(text)                                       # Name (score)
    if m:
        name = _player_name(m.group(1))
        if name:
            return name, _score(m.group(2)), None
    return None, None, None


def _mvp_narrative(node, title: str) -> str | None:
    anc = node.parent
    for _ in range(5):
        if anc is None:
            return None
        full = anc.get_text(" ", strip=True)
        if len(full) > len(title) + 5 and title in full:
            return full.split(title, 1)[1].strip() or None
        anc = anc.parent
    return None


# A Bola's newer "as notas" layout drops the "O melhor em campo" label entirely:
# the standout player sits alone in a coloured highlight card (a yellow CSS
# linear-gradient box) whose bold line is "(score) Name" with the narrative
# beside it. There's no label text to match and the card is a <div>/<span> the
# ratings walk (h1-h6/<p> only) never visits, so without this the highlighted
# player -- usually the match's best (e.g. "(7) Pedro Gonçalves" in "...as notas
# do Sporting") -- is dropped completely. Find the card by its gradient styling.
_GRADIENT_RE = re.compile(r"linear-gradient", re.I)
_BOLD_CLASS_RE = re.compile(r"font-bold", re.I)


def _highlight_cards(soup) -> list[dict]:
    """Standout-player highlight cards (no 'melhor em campo' label) -> figura
    boxes. A card is a gradient-backed box whose bold line reads '(score) Name';
    the rest of the box text is the narrative. Same-gradient promo cards (related
    news, headed '// Nacional // …') are skipped -- their bold line isn't a
    '(score) Name' rating, so the leading-parenthesis match fails."""
    out = []
    for box in soup.find_all(True, class_=_GRADIENT_RE):
        bold = box.find(["strong", "b"]) or box.find("span", class_=_BOLD_CLASS_RE)
        if not bold:
            continue
        head = bold.get_text(" ", strip=True)
        m = re.match(r"\(\s*(" + _RATING + r")\s*\)\s*(.+)", head)
        if not m:
            continue
        name = _player_name(m.group(2))
        if not name:
            continue
        full = box.get_text(" ", strip=True)
        desc = full.split(head, 1)[1].strip(" :—–-") if head in full else ""
        out.append({"kind": "figura", "team": None, "name": name,
                    "score": _score(m.group(1)), "description": desc or None})
    return out


def _mvp_boxes(soup) -> list[dict]:
    """Find 'O melhor em campo' / 'A figura' boxes -> name, score, team, text.
    Three labelled layouts: label-first ("O melhor em campo: Name (7)"),
    label-last ("Prestianni (7) — o melhor em campo"), and the visual "square"
    where the label and the player line ("7 - Francisco Trincão — …") sit in
    separate spans of one box -- plus the unlabelled gradient highlight card
    (_highlight_cards), A Bola's newer way of flagging the standout player."""
    boxes, seen = [], set()
    for node in soup.find_all(string=re.compile(r"melhor em campo|a figura", re.I)):
        lbl = re.search(r"(?:o\s+)?melhor em campo|a figura", node, re.I)
        name = score = team = narrative = None
        m = MVP_RE.search(node)
        if m:
            name, paren = _clean_name(m.group(3)), m.group(4).strip()
            if re.fullmatch(r"(?:nota\s*)?[\d\-–—]+", paren, re.I):
                score, team = _score(paren), (m.group(2) or "").strip() or None
            else:  # the parenthesis is the team, not a score
                score, team = None, (m.group(2) or paren).strip() or None
            narrative = _mvp_narrative(node, m.group(0))
        elif (m := MVP_SUFFIX_RE.search(node)):
            name, score = _clean_name(m.group(1)), _score(m.group(2))
            narrative = _mvp_narrative(node, m.group(0))
        elif re.match(r"\s*(?:(?:o\s+)?melhor em campo|a figura)\b", node, re.I):
            # "square": label here, player line in a sibling span of the box.
            box = node.parent
            for _ in range(4):
                if box is None:
                    break
                after = re.split(r"(?i)(?:o\s+)?melhor em campo|a figura",
                                 box.get_text(" ", strip=True), maxsplit=1)
                if len(after) > 1:
                    name, score, narrative = \
                        _score_name_from_text(after[1].lstrip(" :—–-"))
                    if name:
                        break
                box = box.parent
        if not name:
            continue
        key = _norm(name)
        if key in seen:
            continue
        seen.add(key)
        boxes.append({
            "kind": "melhor" if (lbl and "melhor" in lbl.group(0).lower()) else "figura",
            "team": team, "name": name, "score": score, "description": narrative,
        })
    # Unlabelled gradient highlight cards (newer layout). Deduped against the
    # labelled boxes above so a page carrying both never lists the player twice.
    for card in _highlight_cards(soup):
        key = _norm(card["name"])
        if key not in seen:
            seen.add(key)
            boxes.append(card)
    return boxes


def _canonical_team(teams: dict, name: str, default_team: str | None = None) -> str:
    """Reuse an existing bucket when `name` is the same club under a variant
    spelling, so a single page never splits one team across two buckets.
    First by exact/substring ('Estoril' header vs default 'Estoril Praia'); then,
    ONLY against the page's default team, by alias tokens -- a big-three notas
    page is titled with A Bola's name ('Aves SAD') while we seeded the bucket
    with zerozero's ('AFS'), and those share no substring but are the same club.
    The alias step is limited to the default team so two genuinely different
    sides in a crónica never merge."""
    n = _norm(name)
    for tn in teams:
        t = _norm(tn)
        if t == n or (min(len(t), len(n)) >= 4 and (t in n or n in t)):
            return tn
    if default_team and default_team in teams \
            and _team_tokens(name) & _team_tokens(default_team):
        return default_team
    return name


def _names_match(a: str, b: str) -> bool:
    """Same player by name: exact, or one a substring of the other when the
    shorter is long enough to be unambiguous ('Ríos' ~ 'Richard Ríos')."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short = na if len(na) <= len(nb) else nb
    return len(short) >= 4 and (na in nb or nb in na)


def _target_team(teams: dict, hint: str | None) -> str | None:
    """Pick the team bucket an MVP box belongs to from its team hint."""
    if hint:
        h, htok = _norm(hint), _team_tokens(hint)
        for tn in teams:
            n = _norm(tn)
            if n == h or h in n or n in h or any(t in n for t in htok):
                return tn
    return next(iter(teams)) if len(teams) == 1 else None


def apply_mvp(teams: dict, boxes: list[dict]) -> None:
    """Resolve MVP across a whole MATCH (both teams / both big-three pages):
    'O melhor em campo' wins; 'A figura' is MVP only when no melhor exists
    anywhere. Marks the matching player, or appends the box if not in the list."""
    has_melhor = any(b["kind"] == "melhor" for b in boxes)
    for b in boxes:
        is_mvp = b["kind"] == "melhor" or (b["kind"] == "figura" and not has_melhor)
        found = False
        for plist in teams.values():
            for pl in plist:
                if _names_match(pl["player_name"], b["name"]):
                    if is_mvp:
                        pl["is_mvp"] = True
                    if not pl.get("description") and b["description"]:
                        pl["description"] = b["description"]
                    if pl.get("score") is None and b["score"] is not None:
                        pl["score"] = b["score"]
                    found = True
        if not found and teams:
            target = _target_team(teams, b["team"])
            if target:
                teams[target].append({
                    "player_name": b["name"], "player_id": None,
                    "score": b["score"], "is_mvp": is_mvp,
                    "description": b["description"],
                })


def _dedupe_players(plist: list[dict]) -> list[dict]:
    """Merge a player listed twice on one page -- some big-three pages carry a
    one-line summary AND a per-player deep dive. Key by (name, score): the two
    representations of one player share both, so they merge (keeping the richer
    description/id); two DIFFERENT players who happen to share a name keep
    distinct scores (Gil Vicente's two 'Zé Carlos', 5 and -), so they survive."""
    merged: dict[tuple, dict] = {}
    order: list[tuple] = []
    for p in plist:
        key = (_norm(p["player_name"]), p["score"])
        if key not in merged:
            merged[key] = p
            order.append(key)
        else:
            ex = merged[key]
            if not ex.get("description") and p.get("description"):
                ex["description"] = p["description"]
            if not ex.get("player_id") and p.get("player_id"):
                ex["player_id"] = p["player_id"]
            ex["is_mvp"] = ex.get("is_mvp") or p.get("is_mvp")
    return [merged[k] for k in order]


def parse_page(html: str, default_team: str | None = None) -> dict:
    """Parse one A Bola ratings page into {teams, mvp_boxes}.

    Walks headings + paragraphs in document order, tracking the 'current' team
    from 'as notas d<team>' headers (which live in <h1>/<h2>/<p>/<strong>
    depending on the article). Ratings are read from element TEXT so players
    A Bola left unlinked are still captured. MVP is NOT applied here -- call
    apply_mvp() once the whole match is assembled (it spans two pages for the
    big three)."""
    soup = BeautifulSoup(html, "lxml")
    teams: dict[str, list] = {}
    current = default_team
    if current:
        teams.setdefault(current, [])

    def add(rows):
        bucket = teams.setdefault(current, [])
        bucket.extend(r for r in rows if r["player_name"])

    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"]):
        if _is_notas_header(el):
            team = _team_of_header(el)
            current = _canonical_team(teams, team, default_team) if team else current
            if current:
                teams.setdefault(current, [])
                rest = _after_header(el)
                if TOKEN_RE.search(rest):  # inline list on the same line
                    add(_inline_entries(rest, _link_id_map(el)))
            continue
        if current:
            rows = _rating_rows(el)
            if rows:
                add(rows)

    for tname in teams:
        teams[tname] = _dedupe_players(teams[tname])

    boxes = _mvp_boxes(soup)
    for b in boxes:  # tag each box with its page so apply_mvp can place it
        b["team"] = b["team"] or default_team
    return {"teams": teams, "mvp_boxes": boxes}


def _pick_team(teams: dict, want: str) -> list:
    """Best-effort match of a parsed team bucket to the wanted team name.
    A Bola's name often differs from zerozero's ('Vitória SC' vs 'Vitória de
    Guimarães', 'AFS' vs 'Aves SAD'), so after exact/substring checks fall back
    to distinctive-token (alias-expanded) overlap and take the best bucket."""
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


def collect_cronicas(dates: list[date], *, pages: int = 25,
                     delay: float = 1.0) -> list[dict]:
    """Fetch & parse EVERY crónica falling in any match's date window, once.
    Returns [{url, date, teams, mvp_boxes}] so several matches can be assigned
    from one pass instead of each re-searching and re-opening the same pages."""
    if not dates:
        return []
    index, earliest = [], min(dates)
    for d, u in abola_search("crónica", pages=pages, until_date=earliest):
        if "cronica" not in u or not any(_in_window(d, g) for g in dates):
            continue
        try:
            page = parse_page(fetch(u))
        except Exception:  # noqa: BLE001
            continue
        index.append({"url": u, "date": d, "teams": page["teams"],
                      "mvp_boxes": page["mvp_boxes"]})
        time.sleep(delay)
    return index


def _match_cronica(index: list[dict], home: str, away: str,
                   gdate: date) -> dict | None:
    """The crónica in `index` whose parsed teams cover both sides of a match."""
    ht, at = _team_tokens(home), _team_tokens(away)
    for e in sorted(index, key=lambda x: x["date"]):
        if not _in_window(e["date"], gdate):
            continue
        names = " ".join(_norm(t) for t in e["teams"])
        if any(t in names for t in ht) and any(t in names for t in at):
            return e
    return None


def _parse_team_pages(pairs, urls_used: list, delay: float) -> tuple[dict, list]:
    """Parse a list of (url, team) single-team notas pages into a merged
    (teams, boxes), appending each fetched url to `urls_used`. Skips falsy or
    already-used urls."""
    teams: dict[str, list] = {}
    boxes: list[dict] = []
    for url, want in pairs:
        if not url or url in urls_used:
            continue
        urls_used.append(url)
        page = parse_page(fetch(url), default_team=want)
        for name, plist in page["teams"].items():
            teams.setdefault(name, []).extend(plist)
        boxes.extend(page["mvp_boxes"])
        time.sleep(delay)
    return teams, boxes


def scrape_match(match: dict, *, single_url: str | None = None,
                 home_url: str | None = None, away_url: str | None = None,
                 cronica: dict | None = None, delay: float = 1.0) -> dict:
    """Scrape one match's A Bola ratings into the merged output structure.

    URLs may be injected (skips search) for testing/resilience; `cronica` is a
    pre-parsed entry from collect_cronicas() for the non-big-three path.
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
        # Two separate "as notas" pages; MVP ('melhor' beats 'figura') spans
        # both, so combine their teams + boxes and resolve once.
        if home_url is None and single_url is None:
            home_url = find_team_notas(home, gdate)
            time.sleep(delay)
        if away_url is None and single_url is None:
            away_url = find_team_notas(away, gdate)
        teams, boxes = _parse_team_pages(
            [(home_url, home), (away_url, away)], out["urls_used"], delay)
        apply_mvp(teams, boxes)
        out["home_team_ratings"] = _pick_team(teams, home)
        out["away_team_ratings"] = _pick_team(teams, away)
    else:
        if cronica is None and single_url is None:
            single_url = find_cronica(home, away, gdate)
        if cronica is not None:
            out["urls_used"].append(cronica["url"])
            teams, boxes = cronica["teams"], cronica["mvp_boxes"]
        elif single_url:
            out["urls_used"].append(single_url)
            page = parse_page(fetch(single_url))
            teams, boxes = page["teams"], page["mvp_boxes"]
        else:
            teams, boxes = {}, []
        if teams:
            apply_mvp(teams, boxes)
            out["home_team_ratings"] = _pick_team(teams, home)
            out["away_team_ratings"] = _pick_team(teams, away)
        # Not every non-big-three match gets a combined crónica: notable games
        # (e.g. the Minho derby) use the big-three TWO-page format instead -- one
        # "as notas do X" per team. So when a side is still missing, fill it from
        # its own notas page (found via search/feed). Skipped when URLs/crónica
        # were injected (batch/tests decide their own sources).
        if cronica is None and single_url is None \
                and (not out["home_team_ratings"] or not out["away_team_ratings"]):
            pairs = []
            if not out["home_team_ratings"]:
                pairs.append((find_team_notas(home, gdate), home))
                time.sleep(delay)
            if not out["away_team_ratings"]:
                pairs.append((find_team_notas(away, gdate), away))
            teams2, boxes2 = _parse_team_pages(pairs, out["urls_used"], delay)
            if teams2:
                apply_mvp(teams2, boxes2)
                if not out["home_team_ratings"]:
                    out["home_team_ratings"] = _pick_team(teams2, home)
                if not out["away_team_ratings"]:
                    out["away_team_ratings"] = _pick_team(teams2, away)

    return out


def scrape_matches(matches: list[dict], *, delay: float = 1.0) -> list[dict]:
    # Pre-collect crónicas covering the non-big-three matches' dates, so each is
    # opened once and assigned by the teams found inside (per A Bola, crónica
    # titles rarely name the teams).
    cron_dates = [_parse_date(m["game_date"]) for m in matches
                  if not m.get("is_big_three_match") and m.get("game_date")]
    index = collect_cronicas(cron_dates, delay=delay) if cron_dates else []

    results = []
    for i, m in enumerate(matches, 1):
        print(f"[{i}/{len(matches)}] {m['home_team']} vs {m['away_team']}",
              file=sys.stderr)
        try:
            cron = None
            if not m.get("is_big_three_match"):
                cron = _match_cronica(index, m["home_team"], m["away_team"],
                                      _parse_date(m["game_date"]))
            results.append(scrape_match(m, cronica=cron, delay=delay))
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
