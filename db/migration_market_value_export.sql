-- Migration: expose players.market_value_k on the backoffice roster view so the
-- competition export can ship it to subscribers (e.g. Liga Lendária).
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- Re-creates competition_player_details (mirrors db/migration_player_photo.sql)
-- with p.market_value_k added. Requires db/migration_market_value.sql first
-- (adds players.market_value_k).

drop view if exists public.competition_player_details;
create view public.competition_player_details
with (security_invoker = on) as
select
    rm.competition_id,
    rm.team_id,
    t.name              as team_name,
    rm.player_id,
    p.name              as player_name,
    coalesce(rm.age_at_sync, p.age) as age,
    rm.position_group,
    p.position,
    p.position_code,
    p.club_name,
    p.photo_url,
    p.market_value_k,
    rm.shirt_number,
    rm.active,
    p.source_url,
    greatest(rm.updated_at, p.updated_at) as last_updated
from public.roster_memberships rm
join public.players p on p.id = rm.player_id
left join public.teams t on t.id = rm.team_id;
