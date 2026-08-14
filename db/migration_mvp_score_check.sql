-- Migration: flag matches whose MVP was crowned but never got a score.
-- Run once in the Supabase SQL editor. Safe to re-run (view replace only).
--
-- migration_mvp_flag.sql caught the case where a fetched match had NO player
-- flagged MVP at all. It missed the opposite (and much more common) failure:
-- a rating parses with is_mvp=true but score=null -- a genuine A Bola parsing
-- gap (a new MVP-card layout the scraper doesn't fully handle yet), not an
-- editorial one. That player still shows reporter_linked=true (the NAME
-- matched fine), so the round page's "N unlinked" coverage badge -- which
-- only counts players.length vs linked.length -- never flags it. It was
-- found by chance (Fotis Ioannidis, Sporting-V. Guimarães 2026-08-14: MVP
-- with a "7" nobody read, buried in a narrative box abola.py wasn't pulling
-- a score from) rather than by any worklist.
--
-- Rebuild reporter_link_status (unchanged columns kept, from
-- migration_mvp_flag.sql) with one more, derived the same way -- straight
-- from the is_mvp/score already stored on each rating in
-- matches_reporter_link.home_ratings/away_ratings, no crawler change:
--   mvp_missing_score -- at least one is_mvp=true rating has score=null
-- Unlike has_mvp=false (sometimes a genuine "no standout" article), this one
-- is never legitimate -- A Bola always gives its MVP a number -- so it's
-- worth surfacing as "likely a scraper bug", separate from the "check by
-- hand" MVP-missing list.

drop view if exists public.reporter_link_status;
create view public.reporter_link_status
with (security_invoker = on) as
select
    m.id                                                  as match_id,
    m.round,
    m.played_on,
    count(mp.*) filter (where not mp.did_not_play)        as players,
    count(mp.*) filter (where not mp.did_not_play
                          and mp.reporter_linked)         as linked,
    (rl.match_id is not null)                             as fetched,
    coalesce(jsonb_array_length(rl.home_ratings), 0)
      + coalesce(jsonb_array_length(rl.away_ratings), 0) > 0
                                                            as has_ratings,
    exists (
        select 1
          from jsonb_array_elements(
                   coalesce(rl.home_ratings, '[]'::jsonb)
                   || coalesce(rl.away_ratings, '[]'::jsonb)
               ) as elem
         where (elem ->> 'is_mvp')::boolean
    )                                                      as has_mvp,
    exists (
        select 1
          from jsonb_array_elements(
                   coalesce(rl.home_ratings, '[]'::jsonb)
                   || coalesce(rl.away_ratings, '[]'::jsonb)
               ) as elem
         where (elem ->> 'is_mvp')::boolean
           and (elem ->> 'score') is null
    )                                                      as mvp_missing_score
from public.matches m
join public.match_players mp on mp.match_id = m.id
left join public.matches_reporter_link rl on rl.match_id = m.id
group by m.id, m.round, m.played_on, rl.match_id, rl.home_ratings, rl.away_ratings;
