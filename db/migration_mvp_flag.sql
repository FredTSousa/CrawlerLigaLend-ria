-- Migration: flag matches whose reporter ratings were fetched successfully
-- but carry no MVP at all, so they can be reviewed by hand.
-- Run once in the Supabase SQL editor. Safe to re-run (view replace only).
--
-- Some source crónicas genuinely never name a standout player (e.g. a messy
-- derby recap that's all narrative, no "melhor em campo"/highlight box) --
-- that's not a parsing bug, but it IS a gap the user wants surfaced so they
-- can check the printed edition and set the MVP by hand (the ★ MVP toggle on
-- the match page already does this, via reporter_set_mvp -- see
-- db/migration_set_mvp.sql). Without a flag, a match with no MVP looks
-- identical to one that was never checked.
--
-- reporter_link_status already carries one row per match with ratings
-- fetched; this rebuild adds two columns derived straight from the stored
-- home_ratings/away_ratings (no new table, no crawler change needed --
-- every rating already carries an is_mvp flag, see matches_reporter_link):
--   has_ratings -- at least one rating was actually fetched (home or away)
--   has_mvp     -- at least one of those fetched ratings is flagged is_mvp
-- A match "needs an MVP check" when fetched AND has_ratings AND NOT has_mvp.

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
    )                                                      as has_mvp
from public.matches m
join public.match_players mp on mp.match_id = m.id
left join public.matches_reporter_link rl on rl.match_id = m.id
group by m.id, m.round, m.played_on, rl.match_id, rl.home_ratings, rl.away_ratings;
