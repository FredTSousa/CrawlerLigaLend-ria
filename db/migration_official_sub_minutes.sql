-- Migration: compare raw sub minutes on /official-diff, not only the under-20'
-- flag they drive. Run once in the Supabase SQL editor, AFTER
-- db/migration_official_no_assists.sql. Safe to re-run (only the view is
-- rebuilt; no data is touched).

drop view if exists public.official_stat_diff;
create view public.official_stat_diff
with (security_invoker = true) as
with synced as (
    select id, competition_id, round from public.matches
    where official_synced_at is not null
),
z as (
    select mp.*, s.competition_id, s.round
    from public.match_players mp
    join synced s on s.id = mp.match_id
),
o as (
    select omp.*, s.competition_id, s.round
    from public.official_match_players omp
    join synced s on s.id = omp.match_id
    where omp.player_id is not null
),
j as (
    select
        coalesce(o.match_id, z.match_id)             as match_id,
        coalesce(o.player_id, z.player_id)           as player_id,
        coalesce(o.team_id, z.team_id)               as team_id,
        coalesce(o.competition_id, z.competition_id) as competition_id,
        coalesce(o.round, z.round)                   as round,
        (z.match_id is null)                         as zz_missing,
        coalesce(z.goals, 0) + coalesce(z.penalties_scored, 0) as zz_goals,
        coalesce(o.goals, 0)                         as off_goals,
        coalesce(z.own_goals, 0)                     as zz_own_goals,
        coalesce(o.own_goals, 0)                     as off_own_goals,
        coalesce(z.yellow_cards, 0)                  as zz_yellow_cards,
        coalesce(o.yellow_cards, 0)                  as off_yellow_cards,
        coalesce(z.red_card, false)                  as zz_red_card,
        coalesce(o.red_card, false)                  as off_red_card,
        coalesce(z.played_under_20m, false)          as zz_under_20m,
        coalesce(o.played_under_20m, false)          as off_under_20m,
        z.entered_min as zz_entered_min, z.left_min as zz_left_min,
        o.entered_min as off_entered_min, o.left_min as off_left_min,
        coalesce(z.stat_overrides, '{}'::jsonb)      as stat_overrides
    from o
    full join z on z.match_id = o.match_id and z.player_id = o.player_id
)
select
    j.*,
    (zz_entered_min is distinct from off_entered_min
     or zz_left_min is distinct from off_left_min) as minutes_differ,
    (zz_goals <> off_goals or zz_own_goals <> off_own_goals
     or zz_yellow_cards <> off_yellow_cards or zz_red_card <> off_red_card
     or zz_under_20m <> off_under_20m
     or zz_entered_min is distinct from off_entered_min
     or zz_left_min is distinct from off_left_min) as differs
from j;
