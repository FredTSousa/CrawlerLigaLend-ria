-- Exclude did_not_play players from reporter coverage counts and the
-- match_player_details view. Players who didn't play will never receive
-- reporter scores and should not count as unlinked.

-- 1) Rebuild reporter_link_status to ignore did_not_play rows.
drop view if exists public.reporter_link_status;
create view public.reporter_link_status
with (security_invoker = on) as
select
    m.id                                                  as match_id,
    m.round,
    count(mp.*) filter (where not mp.did_not_play)        as players,
    count(mp.*) filter (where not mp.did_not_play
                          and mp.reporter_linked)         as linked,
    (rl.match_id is not null)                             as fetched
from public.matches m
join public.match_players mp on mp.match_id = m.id
left join public.matches_reporter_link rl on rl.match_id = m.id
group by m.id, m.round, rl.match_id;

-- 2) Rebuild match_player_details to expose did_not_play so the UI can
--    suppress the ⚠ warning chip for those players.
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
    mp.did_not_play,
    mp.reporter_score,
    mp.reporter_is_mvp,
    mp.reporter_linked,
    mp.reporter_manual
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;
