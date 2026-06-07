-- Migration: player headshot URL.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- zerozero's team (squad) page already carries each player's headshot as a CSS
-- background-image on <div class="photo">, so roster.py reads it inline with the
-- fast roster crawl — no per-player page needed. Stored here for later use
-- (rosters UI, exported PlayerUpdated/RosterUpdated snapshots).

alter table public.players
    add column if not exists photo_url text;   -- zerozero squad-page headshot

-- Re-create the backoffice roster view so the headshot is selectable there
-- (mirrors db/migration_competition_squads.sql section 7, plus p.photo_url).
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
    rm.shirt_number,
    rm.active,
    p.source_url,
    greatest(rm.updated_at, p.updated_at) as last_updated
from public.roster_memberships rm
join public.players p on p.id = rm.player_id
left join public.teams t on t.id = rm.team_id;
