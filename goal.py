#!/usr/bin/env python3
"""
Goal.com player-ratings scraper.

A second reporter-rating source alongside abola.py. Goal.com is a Next.js site:
every field we need is in the page's `<script id="__NEXT_DATA__">` JSON, so no
headless browser is required.

Flow (driven by reporter_sync when a competition's rating_source = GOAL):
  1. discover_match_url() — open the competition's configured fixtures/results
     page, find the Goal match matching our internal match (date + home/away),
     and return its Goal URL + id.
  2. scrape_match_url() — open that match page and read every player's RAW Goal
     rating from the lineup (`lineups.teamA|teamB.{lineup,substitutes}[].score`).

Output mirrors abola.scrape_match() so the reporter pipeline stays provider-
agnostic, except each rating carries `raw_score` (the decimal Goal rating) and
`score` left None — the percentile -> 0-10 conversion happens pipeline-side
(percentile.py) against the competition's stored distribution + mapping.

Goal has no explicit MVP flag, so the single highest-scored player in the match
is marked is_mvp.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

# Reuse A Bola's name/team helpers + HTTP conventions so matching behaves the
# same across providers (NFKD normalise, distinctive team tokens, retrying fetch).
from abola import _norm, _slug, _team_tokens, fetch

GOAL_BASE = "https://www.goal.com"

# Kickoffs stored as a plain date (zerozero) vs Goal's ISO UTC instant can land a
# day apart for late/early kickoffs, so allow a one-day slack when date-matching.
MAX_DATE_SLACK_DAYS = 1

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


# ----------------------------------------------------------------------------
# __NEXT_DATA__ extraction
# ----------------------------------------------------------------------------


def _next_data(html: str) -> dict:
    """Parse the Next.js __NEXT_DATA__ JSON blob out of a Goal.com page."""
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise RuntimeError("Goal page has no __NEXT_DATA__ script")
    return json.loads(m.group(1))


def _content(html: str) -> dict:
    return _next_data(html).get("props", {}).get("pageProps", {}).get("content", {}) or {}


def _iso_date(s: str | None):
    """'2026-06-11T19:00:00.000Z' -> date. None on anything unparseable."""
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_date(s: str):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


# ----------------------------------------------------------------------------
# Team matching
# ----------------------------------------------------------------------------


def _team_match_score(goal_team: dict, want: str) -> int:
    """How well a Goal team object matches the wanted (zerozero) team name:
    3 exact code/name, 1 distinctive-token overlap, 0 none. Goal uses English
    names ('Mexico', 'South Africa') + a 3-letter codeName ('MEX'); zerozero may
    use a localized name, so compare on accent-stripped tokens too."""
    name = goal_team.get("name") or ""
    code = goal_team.get("codeName") or ""
    nn, wn = _norm(name), _norm(want)
    if nn and nn == wn:
        return 3
    if code and _norm(code) == wn:
        return 3
    return 1 if _team_tokens(name) & _team_tokens(want) else 0


def _pair_score(ta: dict, tb: dict, home: str, away: str) -> int:
    """Best combined score for assigning (teamA, teamB) to (home, away) in either
    orientation. 0 unless BOTH sides match in the chosen orientation."""
    straight = min(_team_match_score(ta, home), _team_match_score(tb, away))
    swapped = min(_team_match_score(ta, away), _team_match_score(tb, home))
    return max(straight, swapped)


# ----------------------------------------------------------------------------
# Discovery — find the Goal match URL from the fixtures/results page
# ----------------------------------------------------------------------------


def _fixtures_matches(content: dict) -> list[dict]:
    """All match objects across the fixtures page's gamesets."""
    out: list[dict] = []
    for gs in content.get("gamesets") or []:
        out.extend(gs.get("matches") or [])
    return out


def _match_url(goal_id: str, home_name: str, away_name: str) -> str:
    """Canonical Goal match URL. The slug is cosmetic (Goal resolves by id), but
    we build the real one from the teams' English names for clean, stable links."""
    slug = f"{_slug(home_name)}-vs-{_slug(away_name)}" or "match"
    return f"{GOAL_BASE}/en/match/{slug}/{goal_id}"


def discover_match_url(fixtures_url: str, home: str, away: str,
                       game_date) -> tuple[str | None, str | None]:
    """Find the Goal match URL+id for our internal match on the configured
    fixtures/results page, matching by date (+/- a day) and both team names.
    Returns (url, id) or (None, None) if no confident match is found.

    `game_date` may be a date or 'YYYY-MM-DD' string."""
    gdate = game_date if hasattr(game_date, "year") else _parse_date(str(game_date))
    content = _content(fetch(fixtures_url))
    best, best_score = None, 0
    for mt in _fixtures_matches(content):
        ta, tb = mt.get("teamA") or {}, mt.get("teamB") or {}
        mdate = _iso_date(mt.get("startDate"))
        if mdate is None or abs((mdate - gdate).days) > MAX_DATE_SLACK_DAYS:
            continue
        score = _pair_score(ta, tb, home, away)
        if score > best_score:
            best, best_score = mt, score
    if not best or best_score < 1:
        return None, None
    gid = best.get("id")
    if not gid:
        return None, None
    ta, tb = best.get("teamA") or {}, best.get("teamB") or {}
    return _match_url(gid, ta.get("name") or home, tb.get("name") or away), gid


# ----------------------------------------------------------------------------
# Extraction — read raw ratings off a Goal match page
# ----------------------------------------------------------------------------


def _player_ratings(side: dict) -> list[dict]:
    """Raw ratings for one team's lineup (starters + substitutes). A player with
    no score is skipped (Goal only rates those who played enough)."""
    rows: list[dict] = []
    for key in ("lineup", "substitutes"):
        for pl in side.get(key) or []:
            person = pl.get("person") or {}
            name = (person.get("name") or "").strip()
            raw = pl.get("score")
            if not name or raw is None:
                continue
            rows.append({
                "player_name": name,
                "player_id": person.get("id"),
                "raw_score": raw,
                "score": None,            # filled by the percentile conversion
                "is_mvp": False,
                "description": None,
            })
    return rows


def _mark_mvp(teams: dict[str, list[dict]]) -> None:
    """Goal has no MVP flag -> the single highest raw score across the match is
    the MVP. Ties: the first encountered keeps it."""
    best, best_row = None, None
    for plist in teams.values():
        for r in plist:
            try:
                v = float(r["raw_score"])
            except (TypeError, ValueError):
                continue
            if best is None or v > best:
                best, best_row = v, r
    if best_row is not None:
        best_row["is_mvp"] = True


def scrape_match_url(url: str) -> dict:
    """Parse a Goal match page into {teams: {team_name: [ratings]}, team_a,
    team_b}. Ratings carry raw_score (decimal); score is None until converted."""
    match = _content(fetch(url)).get("match") or {}
    lineups = match.get("lineups") or {}
    ta_name = (match.get("teamA") or {}).get("name") or "Team A"
    tb_name = (match.get("teamB") or {}).get("name") or "Team B"
    teams = {
        ta_name: _player_ratings(lineups.get("teamA") or {}),
        tb_name: _player_ratings(lineups.get("teamB") or {}),
    }
    _mark_mvp(teams)
    return {"teams": teams, "team_a": ta_name, "team_b": tb_name}


def _pick_team(teams: dict, want: str) -> list[dict]:
    """The parsed bucket best matching `want` (exact, then token overlap)."""
    wn = _norm(want)
    for name, plist in teams.items():
        if _norm(name) == wn:
            return plist
    best, best_score = [], 0
    wt = _team_tokens(want)
    for name, plist in teams.items():
        score = len(wt & _team_tokens(name))
        if score > best_score:
            best, best_score = plist, score
    return best


# ----------------------------------------------------------------------------
# Orchestration — one match into the common reporter output shape
# ----------------------------------------------------------------------------


def scrape_match(match: dict, *, fixtures_url: str | None = None,
                 goal_url: str | None = None, goal_id: str | None = None) -> dict:
    """Scrape one match's Goal ratings into the shared reporter structure.

    Uses a cached `goal_url` when given (skips fixtures discovery); otherwise
    discovers it from `fixtures_url`. The discovered URL/id are returned under
    `goal_match_url`/`goal_match_id` so the caller can persist them.
    """
    home, away = match["home_team"], match["away_team"]
    gdate = match["game_date"]

    out = {
        "match": f"{home} vs {away}",
        "date": gdate,
        "format_detected": None,
        "urls_used": [],
        "home_team_ratings": [],
        "away_team_ratings": [],
        "goal_match_url": goal_url,
        "goal_match_id": goal_id,
    }

    url = goal_url
    if not url:
        if not fixtures_url:
            raise RuntimeError("GOAL competition has no goal_fixtures_url configured")
        url, gid = discover_match_url(fixtures_url, home, away, gdate)
        out["goal_match_url"], out["goal_match_id"] = url, gid
    if not url:
        return out  # match not found on the fixtures page (left for a retry)

    out["urls_used"].append(url)
    page = scrape_match_url(url)
    teams = page["teams"]
    out["home_team_ratings"] = _pick_team(teams, home)
    out["away_team_ratings"] = _pick_team(teams, away)
    return out


def scrape_matches(matches: list[dict], *, fixtures_url: str | None = None,
                   delay: float = 1.0) -> list[dict]:
    """Scrape several matches off one fixtures page (one discovery fetch each).
    Mirrors abola.scrape_matches so the provider wrapper can batch a round."""
    import time
    results = []
    for i, m in enumerate(matches, 1):
        print(f"[{i}/{len(matches)}] {m['home_team']} vs {m['away_team']}",
              file=sys.stderr)
        try:
            results.append(scrape_match(
                m, fixtures_url=fixtures_url,
                goal_url=m.get("goal_match_url"), goal_id=m.get("goal_match_id")))
        except Exception as err:  # noqa: BLE001
            print(f"  ! failed: {err}", file=sys.stderr)
            results.append({"match": f"{m['home_team']} vs {m['away_team']}",
                            "date": m.get("game_date"), "error": str(err)})
        time.sleep(delay)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", help="A Goal match URL to scrape ratings from")
    ap.add_argument("--fixtures", help="Fixtures URL (for --discover)")
    ap.add_argument("--discover", nargs=3, metavar=("HOME", "AWAY", "DATE"),
                    help="Discover a match URL: home away YYYY-MM-DD")
    args = ap.parse_args()

    if args.discover:
        home, away, d = args.discover
        url, gid = discover_match_url(args.fixtures, home, away, d)
        print(json.dumps({"url": url, "id": gid}, ensure_ascii=False, indent=2))
    elif args.url:
        print(json.dumps(scrape_match_url(args.url), ensure_ascii=False, indent=2))
    else:
        ap.error("pass --url or --discover")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
