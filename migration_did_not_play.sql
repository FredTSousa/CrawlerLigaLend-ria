-- Add did_not_play flag to match_players so bench players who never entered
-- the match are stored (for live "might come in" display) rather than dropped.
alter table match_players
  add column if not exists did_not_play boolean not null default false;

-- Rebuild the view to expose did_not_play so reporter_sync can filter it out.
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
    mp.reporter_raw_score,
    mp.reporter_is_mvp,
    mp.reporter_linked,
    mp.reporter_manual
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;
