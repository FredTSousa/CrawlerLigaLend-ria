#!/usr/bin/env python3
"""
Reporter-rating provider abstraction.

A competition picks its rating source (competitions.rating_source). Each provider
turns a match into the same output shape so reporter_sync's persistence + linking
stay provider-agnostic:

    {match, date, format_detected, urls_used,
     home_team_ratings, away_team_ratings[, goal_match_url, goal_match_id]}

with each rating: {player_name, player_id, raw_score, score, is_mvp, description}.

  * ABOLA -> ABolaRatingProvider: today's abola.py, unchanged. Its `score` is
    already the final 0-10 integer, so raw_score is set equal to it (no percentile
    conversion happens for A Bola).
  * GOAL  -> GoalRatingProvider: goal.py. Emits raw_score (a decimal) with score
    left None; the pipeline converts raw -> 0-10 via percentile.py against the
    competition's stored distribution + configured mapping.

Add a future source by writing one more RatingProvider and a branch in
get_provider(); nothing else in the pipeline needs to change.
"""

from __future__ import annotations

import abola
import goal as goal_mod

ABOLA = "ABOLA"
GOAL = "GOAL"


class RatingProvider:
    """Common interface. `source` is stamped on crawl_runs.source so the runs
    pages can tell A Bola from Goal."""

    source = "reporter"

    def scrape_match(self, match: dict, *, delay: float = 1.0, **kw) -> dict:
        raise NotImplementedError

    def scrape_matches(self, matches: list[dict], *, delay: float = 1.0,
                       **kw) -> list[dict]:
        raise NotImplementedError


def _with_raw_equal_score(data: dict) -> dict:
    """For a provider whose `score` is already final (A Bola), mirror it into
    `raw_score` so the persistence layer always has a raw value to store."""
    for key in ("home_team_ratings", "away_team_ratings"):
        for r in data.get(key) or []:
            r.setdefault("raw_score", r.get("score"))
    return data


class ABolaRatingProvider(RatingProvider):
    source = "abola"

    def scrape_match(self, match: dict, *, delay: float = 1.0,
                     home_url: str | None = None, away_url: str | None = None,
                     single_url: str | None = None, **kw) -> dict:
        # home_url/away_url/single_url let a caller pin the exact A Bola page(s)
        # (a UI override when discovery picked the wrong one); an unset side is
        # still discovered normally inside abola.scrape_match.
        return _with_raw_equal_score(abola.scrape_match(
            match, delay=delay, home_url=home_url, away_url=away_url,
            single_url=single_url))

    def scrape_matches(self, matches: list[dict], *, delay: float = 1.0,
                       **kw) -> list[dict]:
        return [_with_raw_equal_score(d) if not d.get("error") else d
                for d in abola.scrape_matches(matches, delay=delay)]


class GoalRatingProvider(RatingProvider):
    source = "goal"

    def __init__(self, fixtures_url: str | None = None):
        self.fixtures_url = fixtures_url

    def scrape_match(self, match: dict, *, delay: float = 1.0,
                     goal_url: str | None = None, goal_id: str | None = None,
                     **kw) -> dict:
        return goal_mod.scrape_match(
            match, fixtures_url=self.fixtures_url,
            goal_url=goal_url or match.get("goal_match_url"),
            goal_id=goal_id or match.get("goal_match_id"))

    def scrape_matches(self, matches: list[dict], *, delay: float = 1.0,
                       **kw) -> list[dict]:
        return goal_mod.scrape_matches(
            matches, fixtures_url=self.fixtures_url, delay=delay)


def get_provider(rating_source: str | None, *,
                 goal_fixtures_url: str | None = None) -> RatingProvider:
    """Pick the provider for a competition's configured rating_source.
    Defaults to A Bola (the historical behaviour) for null/unknown values."""
    if (rating_source or ABOLA).upper() == GOAL:
        return GoalRatingProvider(fixtures_url=goal_fixtures_url)
    return ABolaRatingProvider()
