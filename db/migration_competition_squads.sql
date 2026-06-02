-- Migration: competition squads — Teams & Players owned by a Competition.
-- Run once in the Supabase SQL editor. Safe to re-run (additive + idempotent).
--
-- Extends the fixture-only model so a Competition (which, because competitions.id
-- IS zerozero's per-season id_edicao, is already a *competition-season*) owns:
--   competition_teams   — which teams are in this season's edition (membership)
--   roster_memberships  — which players are in each team's squad that season,
--                         grouped by position_group, with soft-delete history
--
-- teams & players stay GLOBAL (their zerozero ids are stable across seasons);
-- only the join tables are season-scoped. Nothing here breaks fixtures or the
-- existing subscriptions fan-out. See docs/COMPETITION_BACKOFFICE_DESIGN.md.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Competition: enrich the existing row (do NOT recreate — matches/subscribers
--    reference competitions.id). id already == zerozero id_edicao; slug already
--    groups a league across seasons, so no separate zerozero_id column is added.
-- ----------------------------------------------------------------------------
alter table public.competitions
    add column if not exists full_name    text,        -- "Liga Portugal Betclic 2025/26"
    add column if not exists season        text,        -- "2025/26"
    add column if not exists epoca_id       text,        -- season id used in /equipa/<id>?epoca_id=
    add column if not exists source_url     text,        -- /competicao/<slug>
    add column if not exists last_sync_at   timestamptz,
    add column if not exists teams_count    int,         -- stamped at sync time (avoids count(*))
    add column if not exists players_count  int,
    add column if not exists created_at     timestamptz not null default now();

-- ----------------------------------------------------------------------------
-- 2) Teams: enrich the existing thin row.
-- ----------------------------------------------------------------------------
alter table public.teams
    add column if not exists slug         text,         -- "santa-clara"
    add column if not exists logo_url     text,
    add column if not exists source_url   text,         -- /equipa/<slug>/<id>
    add column if not exists last_sync_at timestamptz,
    add column if not exists created_at   timestamptz not null default now();

-- ----------------------------------------------------------------------------
-- 3) Players: enrich with stable attributes + a "latest" snapshot of the
--    detailed position. Season-varying truth (group, shirt, age-at-time) lives
--    on roster_memberships; these columns are the convenient current values.
-- ----------------------------------------------------------------------------
alter table public.players
    add column if not exists slug           text,
    add column if not exists birth_date     date,        -- stable; preferred over age
    add column if not exists age            int,         -- latest scraped age (volatile)
    add column if not exists nationality    text,
    add column if not exists position       text,        -- detailed "Posição" (authoritative)
    add column if not exists position_group text,        -- latest roster grouping
    add column if not exists position_code  text,        -- normalized: GK, CB, ... (see roster.py)
    add column if not exists club_name      text,        -- latest club
    add column if not exists source_url     text,        -- /jogador/<slug>/<id>
    add column if not exists last_sync_at   timestamptz,
    add column if not exists enriched_at    timestamptz, -- last time the detail page was opened
    add column if not exists created_at     timestamptz not null default now();

-- ----------------------------------------------------------------------------
-- 4) Competition <-> Team membership (season-scoped via competition_id).
-- ----------------------------------------------------------------------------
create table if not exists public.competition_teams (
    competition_id text not null references public.competitions(id) on delete cascade,
    team_id        text not null references public.teams(id),
    source_url     text,                            -- the team's season page used to crawl
    active         boolean not null default true,   -- false = no longer in this edition
    first_seen_at  timestamptz not null default now(),
    last_sync_at   timestamptz,
    updated_at     timestamptz not null default now(),
    primary key (competition_id, team_id)
);
create index if not exists competition_teams_team_idx on public.competition_teams (team_id);

-- ----------------------------------------------------------------------------
-- 5) Roster membership (player in team in competition-season). History record.
--    Soft-delete only: a departed player gets active=false + left_at, never DELETE.
-- ----------------------------------------------------------------------------
create table if not exists public.roster_memberships (
    competition_id text not null references public.competitions(id) on delete cascade,
    team_id        text not null references public.teams(id),
    player_id      text not null references public.players(id),
    position_group text,                            -- "Guarda Redes"|"Defesa"|"Médio"|"Avançado"
    shirt_number   int,
    age_at_sync    int,                             -- snapshot of age when crawled
    active         boolean not null default true,
    first_seen_at  timestamptz not null default now(),
    left_at        timestamptz,                     -- set when player drops out of the roster
    last_sync_at   timestamptz,
    updated_at     timestamptz not null default now(),
    primary key (competition_id, team_id, player_id)
);
create index if not exists roster_player_idx on public.roster_memberships (player_id);
create index if not exists roster_comp_idx   on public.roster_memberships (competition_id);
create index if not exists roster_team_idx   on public.roster_memberships (team_id);
create index if not exists roster_active_idx on public.roster_memberships (competition_id, team_id) where active;

-- ----------------------------------------------------------------------------
-- 6) RLS: read for allow-listed users; service_role writes bypass RLS.
-- ----------------------------------------------------------------------------
alter table public.competition_teams   enable row level security;
alter table public.roster_memberships  enable row level security;
do $$ declare t text; begin
  foreach t in array array['competition_teams','roster_memberships'] loop
    execute format('drop policy if exists %I on public.%I', t||'_read', t);
    execute format(
      'create policy %I on public.%I for select to authenticated using (public.is_allowed())',
      t||'_read', t);
  end loop;
end $$;

-- ----------------------------------------------------------------------------
-- 7) Convenience view: players in a competition with their team + group, joined
--    for the backoffice Players tab. security_invoker => RLS of the caller applies.
-- ----------------------------------------------------------------------------
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
    rm.shirt_number,
    rm.active,
    p.source_url,
    greatest(rm.updated_at, p.updated_at) as last_updated
from public.roster_memberships rm
join public.players p on p.id = rm.player_id
left join public.teams t on t.id = rm.team_id;

-- ----------------------------------------------------------------------------
-- 8) Counts helper (used to stamp competitions.teams_count/players_count at the
--    end of a sync, so the backoffice never runs count(*) on page load).
-- ----------------------------------------------------------------------------
create or replace function public.refresh_competition_counts(p_competition_id text)
returns void language sql security definer set search_path = public as $$
    update public.competitions c set
        teams_count = (select count(*) from public.competition_teams ct
                       where ct.competition_id = c.id and ct.active),
        players_count = (select count(*) from public.roster_memberships rm
                         where rm.competition_id = c.id and rm.active)
    where c.id = p_competition_id;
$$;
grant execute on function public.refresh_competition_counts(text) to service_role, authenticated;
