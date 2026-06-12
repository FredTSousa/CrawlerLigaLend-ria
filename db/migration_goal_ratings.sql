-- Migration: multi-provider reporter ratings (A Bola + Goal.com).
-- Run once in the Supabase SQL editor. Safe to re-run (additive + idempotent).
--
-- Each competition picks its reporter-rating source. ABOLA keeps today's
-- behaviour unchanged. GOAL discovers the match on Goal.com from a configured
-- fixtures URL, stores the RAW Goal rating (a decimal, e.g. 7.55), and converts
-- it to the 0-10 integer the rest of the app uses via a per-competition (per
-- season), manually-configured RAW-score-range -> score mapping. Conversion is a
-- direct per-rating lookup; the Goal-ratings viewer also shows a percentile, but
-- only as analytical context (it is not used for conversion).

-- Per-competition rating source + Goal config.
alter table public.competitions
    add column if not exists rating_source     text not null default 'ABOLA', -- 'ABOLA' | 'GOAL'
    add column if not exists goal_fixtures_url text,                           -- required when GOAL
    add column if not exists rating_mapping    jsonb;                          -- {"6.5-7":7,...} raw range->score; null -> code default

-- Discovered Goal match URL/id, cached on the per-match reporter row so future
-- crawls skip the fixtures-page search. Lives with the rest of the reporter
-- artifacts (urls, ratings) rather than on matches.
alter table public.matches_reporter_link
    add column if not exists goal_match_url text,
    add column if not exists goal_match_id  text;

-- Raw provider rating per match-player (e.g. 7.55). Queryable so a season-wide
-- percentile distribution can be aggregated. The converted 0-10 value still
-- lands in match_players.reporter_score (added by migration_reporter.sql); for
-- A Bola raw == score (already an integer 0-10).
alter table public.match_players
    add column if not exists reporter_raw_score numeric;

-- Expose reporter_raw_score (and keep reporter_linked) on the UI view.
drop view if exists public.match_player_details;
create view public.match_player_details
with (security_invoker = on) as
select
    mp.match_id,
    m.round,
    m.played_on,
    mp.player_id,
    p.name              as player_name,
    mp.team_id,
    t.name              as team_name,
    mp.order_index,
    mp.shirt_number,
    mp.is_captain,
    mp.is_starter,
    mp.entered_min,
    mp.left_min,
    mp.goals,
    mp.assists,
    mp.yellow_cards,
    mp.red_card,
    mp.own_goals,
    mp.penalties_scored,
    mp.penalties_missed,
    mp.penalties_defended,
    mp.played_under_20m,
    mp.reporter_score,
    mp.reporter_raw_score,
    mp.reporter_is_mvp,
    mp.reporter_linked,
    mp.reporter_manual
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;
