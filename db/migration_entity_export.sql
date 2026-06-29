-- Migration: entity export — fan out Competition/Team/Player/Roster changes.
-- Run once in the Supabase SQL editor (AFTER migration_competition_squads.sql).
-- Safe to re-run.
--
-- The squad-shaped twin of migration_subscriptions.sql. Where delivery_outbox
-- fans out match changes, entity_outbox fans out squad changes:
--
--   write to competition_teams / roster_memberships / players
--        -> AFTER trigger enqueues a coalesced row in entity_outbox
--        -> the same `dispatch` Edge Function builds the snapshot via
--           build_competition_event / build_team_event / build_player_event,
--           HMAC-signs it, and POSTs to every active subscriber of that league.
--
-- Reuses the existing `subscribers` table and the existing HMAC scheme. Events:
--   CompetitionUpdated | TeamUpdated/RosterUpdated | PlayerUpdated | CompetitionSyncCompleted
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) The entity outbox (parallel to delivery_outbox).
-- ----------------------------------------------------------------------------
create table if not exists public.entity_outbox (
    id              bigint generated always as identity primary key,
    entity_type     text not null,              -- 'competition'|'team'|'player'|'comp_sync'
    entity_id       text not null,              -- competition_id / team_id / player_id
    competition_id  text,                        -- routing key (NULL = all leagues)
    status          text not null default 'pending',  -- pending|sending|delivered|failed
    attempts        int  not null default 0,
    last_error      text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    next_attempt_at timestamptz not null default now(),
    delivered_at    timestamptz
);

-- Coalesce while pending: one pending row per (type, entity, competition) so a
-- full sync's burst of roster upserts collapses to one event per team/league.
create unique index if not exists entity_outbox_pending_uq
    on public.entity_outbox (entity_type, entity_id, coalesce(competition_id, ''))
    where status = 'pending';

create index if not exists entity_outbox_drain_idx
    on public.entity_outbox (status, next_attempt_at) where status = 'pending';

-- ----------------------------------------------------------------------------
-- 2) Enqueue helper + triggers (every squad write path feeds this funnel).
-- ----------------------------------------------------------------------------
create or replace function public.enqueue_entity_event(
    p_type text, p_entity_id text, p_competition_id text)
returns void language sql as $$
    insert into public.entity_outbox (entity_type, entity_id, competition_id)
    values (p_type, p_entity_id, p_competition_id)
    on conflict (entity_type, entity_id, coalesce(competition_id, ''))
        where status = 'pending'
        do update set updated_at = now();
$$;

-- competition_teams: membership changed -> the competition's team list changed.
create or replace function public.trg_competition_teams_enqueue()
returns trigger language plpgsql as $$
begin
    perform public.enqueue_entity_event('competition', new.competition_id, new.competition_id);
    return null;
end $$;
drop trigger if exists competition_teams_enqueue on public.competition_teams;
create trigger competition_teams_enqueue
    after insert or update on public.competition_teams
    for each row execute function public.trg_competition_teams_enqueue();

-- roster_memberships: a team's squad changed -> RosterUpdated for that team.
create or replace function public.trg_roster_enqueue()
returns trigger language plpgsql as $$
begin
    perform public.enqueue_entity_event('team', new.team_id, new.competition_id);
    return null;
end $$;
drop trigger if exists roster_enqueue on public.roster_memberships;
create trigger roster_enqueue
    after insert or update on public.roster_memberships
    for each row execute function public.trg_roster_enqueue();

-- players: a player's metadata changed -> PlayerUpdated, fanned to EVERY league
-- the player is rostered in (one outbox row per competition for correct routing).
-- Skip pure bookkeeping churn (only last_sync_at/scraped timestamp changed).
create or replace function public.trg_players_enqueue()
returns trigger language plpgsql as $$
begin
    if (tg_op = 'UPDATE'
        and new.name          is not distinct from old.name
        and new.position       is not distinct from old.position
        and new.position_code  is not distinct from old.position_code
        and new.position_group is not distinct from old.position_group
        and new.age            is not distinct from old.age
        and new.club_name      is not distinct from old.club_name
        and new.nationality    is not distinct from old.nationality
        and new.photo_url      is not distinct from old.photo_url) then
        return null;  -- nothing a subscriber cares about changed
    end if;
    insert into public.entity_outbox (entity_type, entity_id, competition_id)
    select 'player', new.id, rm.competition_id
      from (select distinct competition_id
              from public.roster_memberships
             where player_id = new.id and active) rm
    on conflict (entity_type, entity_id, coalesce(competition_id, ''))
        where status = 'pending' do update set updated_at = now();
    return null;
end $$;
drop trigger if exists players_enqueue on public.players;
create trigger players_enqueue
    after insert or update on public.players
    for each row execute function public.trg_players_enqueue();

-- ----------------------------------------------------------------------------
-- 3) build_*_event(): the snapshots a subscriber receives.
-- ----------------------------------------------------------------------------

-- Shared competition header.
create or replace function public._competition_header(p_competition_id text)
returns jsonb language sql stable as $$
    select case when c.id is null then null else jsonb_build_object(
        'id', c.id, 'full_name', c.full_name, 'name', c.name, 'slug', c.slug,
        'season', c.season, 'fase', c.fase, 'epoca_id', c.epoca_id) end
    from public.competitions c where c.id = p_competition_id;
$$;

create or replace function public.build_competition_event(p_competition_id text)
returns jsonb language sql stable as $$
    select jsonb_build_object(
        'event', 'CompetitionUpdated',
        'sent_at', now(),
        'competition', (select public._competition_header(p_competition_id)),
        'teams', coalesce((
            select jsonb_agg(jsonb_build_object(
                'id', t.id, 'name', t.name, 'slug', t.slug,
                'logo_url', t.logo_url, 'source_url', ct.source_url,
                'active', ct.active) order by t.name)
            from public.competition_teams ct
            join public.teams t on t.id = ct.team_id
            where ct.competition_id = p_competition_id and ct.active), '[]'::jsonb),
        'counts', (select jsonb_build_object(
            'teams', c.teams_count, 'players', c.players_count)
            from public.competitions c where c.id = p_competition_id));
$$;

create or replace function public.build_team_event(p_team_id text, p_competition_id text)
returns jsonb language sql stable as $$
    select jsonb_build_object(
        'event', 'RosterUpdated',
        'sent_at', now(),
        'competition', (select public._competition_header(p_competition_id)),
        'team', (select case when t.id is null then null else jsonb_build_object(
            'id', t.id, 'name', t.name, 'slug', t.slug, 'logo_url', t.logo_url) end
            from public.teams t where t.id = p_team_id),
        'roster', coalesce((
            select jsonb_agg(jsonb_build_object(
                'player_id', rm.player_id,
                'player_name', p.name,
                'position_group', rm.position_group,
                'position', p.position,
                'position_code', p.position_code,
                'age', coalesce(rm.age_at_sync, p.age),
                'club_name', p.club_name,
                'photo_url', p.photo_url,
                'shirt_number', rm.shirt_number,
                'active', rm.active)
                order by rm.position_group nulls last, rm.shirt_number nulls last)
            from public.roster_memberships rm
            join public.players p on p.id = rm.player_id
            where rm.competition_id = p_competition_id
              and rm.team_id = p_team_id), '[]'::jsonb));
$$;

create or replace function public.build_player_event(p_player_id text, p_competition_id text)
returns jsonb language sql stable as $$
    select jsonb_build_object(
        'event', 'PlayerUpdated',
        'sent_at', now(),
        'competition', (select public._competition_header(p_competition_id)),
        'player', (select jsonb_build_object(
            'id', p.id, 'name', p.name, 'slug', p.slug,
            'age', p.age, 'birth_date', p.birth_date, 'nationality', p.nationality,
            'position', p.position, 'position_group', p.position_group,
            'position_code', p.position_code, 'club_name', p.club_name,
            'photo_url', p.photo_url,
            'source_url', p.source_url, 'last_updated', p.updated_at,
            'team_id', (select rm.team_id from public.roster_memberships rm
                        where rm.player_id = p.id and rm.competition_id = p_competition_id
                          and rm.active limit 1))
            from public.players p where p.id = p_player_id));
$$;

create or replace function public.build_comp_sync_event(p_competition_id text)
returns jsonb language sql stable as $$
    select jsonb_build_object(
        'event', 'CompetitionSyncCompleted',
        'sent_at', now(),
        'competition', (select public._competition_header(p_competition_id)),
        'counts', (select jsonb_build_object(
            'teams', c.teams_count, 'players', c.players_count,
            'last_sync_at', c.last_sync_at)
            from public.competitions c where c.id = p_competition_id));
$$;

-- ----------------------------------------------------------------------------
-- 4) claim_entity_outbox_batch(): atomically reserve pending rows (SKIP LOCKED).
-- ----------------------------------------------------------------------------
create or replace function public.claim_entity_outbox_batch(p_limit int default 50)
returns setof public.entity_outbox language plpgsql as $$
begin
    return query
    with picked as (
        select id from public.entity_outbox
        where status = 'pending' and next_attempt_at <= now()
        order by id for update skip locked limit p_limit
    )
    update public.entity_outbox o
       set status = 'sending', attempts = o.attempts + 1, updated_at = now()
      from picked where o.id = picked.id
    returning o.*;
end $$;

-- ----------------------------------------------------------------------------
-- 5) Emit a CompetitionSyncCompleted (called by roster_sync.py via RPC).
-- ----------------------------------------------------------------------------
create or replace function public.emit_comp_sync(p_competition_id text)
returns void language sql security definer set search_path = public as $$
    insert into public.entity_outbox (entity_type, entity_id, competition_id)
    values ('comp_sync', p_competition_id, p_competition_id)
    on conflict (entity_type, entity_id, coalesce(competition_id, ''))
        where status = 'pending' do update set updated_at = now();
$$;
grant execute on function public.emit_comp_sync(text) to service_role;

-- ----------------------------------------------------------------------------
-- 6) RLS + grants.
-- ----------------------------------------------------------------------------
alter table public.entity_outbox enable row level security;
drop policy if exists entity_outbox_read on public.entity_outbox;
create policy entity_outbox_read on public.entity_outbox
    for select to authenticated using (public.is_allowed());

grant execute on function public.build_competition_event(text)        to service_role;
grant execute on function public.build_team_event(text, text)          to service_role;
grant execute on function public.build_player_event(text, text)        to service_role;
grant execute on function public.build_comp_sync_event(text)           to service_role;
grant execute on function public.claim_entity_outbox_batch(int)        to service_role;
grant execute on function public._competition_header(text)             to service_role;

-- ----------------------------------------------------------------------------
-- 7) Admin RPC: replay all entity events for a competition (seed a subscriber).
--    The entity analog of replay_competition() for matches.
-- ----------------------------------------------------------------------------
create or replace function public.replay_competition_entities(p_competition_id text)
returns int language plpgsql security definer set search_path = public as $$
declare n int := 0;
begin
    if not public.is_allowed() then raise exception 'not allowed'; end if;
    -- competition (team list)
    insert into public.entity_outbox (entity_type, entity_id, competition_id)
    values ('competition', p_competition_id, p_competition_id)
    on conflict (entity_type, entity_id, coalesce(competition_id, '')) where status='pending' do nothing;
    -- one team event per active team
    insert into public.entity_outbox (entity_type, entity_id, competition_id)
    select 'team', ct.team_id, ct.competition_id from public.competition_teams ct
     where ct.competition_id = p_competition_id and ct.active
    on conflict (entity_type, entity_id, coalesce(competition_id, '')) where status='pending' do nothing;
    -- one player event per active rostered player
    insert into public.entity_outbox (entity_type, entity_id, competition_id)
    select distinct 'player', rm.player_id, rm.competition_id
      from public.roster_memberships rm
     where rm.competition_id = p_competition_id and rm.active
    on conflict (entity_type, entity_id, coalesce(competition_id, '')) where status='pending' do nothing;
    select count(*) into n from public.entity_outbox
     where competition_id = p_competition_id and status = 'pending';
    return n;
end $$;
grant execute on function public.replay_competition_entities(text) to authenticated;

-- ----------------------------------------------------------------------------
-- 8) Wiring (run AFTER redeploying the `dispatch` Edge Function, which now also
--    drains entity_outbox). The existing Database Webhook / pg_cron backstop on
--    delivery_outbox already invokes dispatch; add the same for entity_outbox:
--
--   create trigger entity_outbox_ping after insert on public.entity_outbox
--     for each row when (new.status = 'pending')
--     execute function public.ping_dispatch();   -- same ping_dispatch as subscriptions
--
--   (The 30s pg_cron 'dispatch-drain' job already covers retries for both.)
-- ----------------------------------------------------------------------------
