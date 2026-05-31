-- Migration: league subscriptions + change fan-out ("outbox").
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- Lets an EXTERNAL webpage subscribe to a competition (league) and receive a
-- webhook every time a match in that league changes (lineups, live score,
-- reporter ratings). The flow is:
--
--   any write to matches / match_players / matches_reporter_link
--        -> AFTER trigger enqueues ONE coalesced row in delivery_outbox
--                 (keyed by match_id while still 'pending', so a burst of
--                  ~30 player upserts collapses into a single pending event)
--        -> Database Webhook (or pg_cron backstop) invokes the `dispatch`
--           Edge Function, which builds the full snapshot via
--           build_match_event(), HMAC-signs it, and POSTs it to every active
--           subscriber of that competition.
--
-- The payload is assembled at SEND time from current state, so coalescing and
-- retries always deliver the latest snapshot (idempotent for the subscriber).
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) Who is subscribed, and to which league
-- ----------------------------------------------------------------------------
-- secret: shared HMAC key. The dispatcher signs each body with it; the
--   subscriber verifies the X-Signature header with the same secret.
-- callback_url: the subscriber's HTTPS ingest endpoint.
-- competition_id: which league (matches.competition_id). NULL = every league.
create table if not exists public.subscribers (
    id             bigint generated always as identity primary key,
    label          text,                         -- human note, e.g. "fantasy-site prod"
    competition_id text references public.competitions(id),
    callback_url   text not null,
    secret         text not null,
    active         boolean not null default true,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index if not exists subscribers_active_comp_idx
    on public.subscribers (competition_id) where active;

-- ----------------------------------------------------------------------------
-- 2) The outbox: one pending row per "dirty" match, drained by the dispatcher
-- ----------------------------------------------------------------------------
-- status: pending -> sending -> delivered | failed
-- attempts / next_attempt_at: retry bookkeeping (the dispatcher spaces retries).
create table if not exists public.delivery_outbox (
    id              bigint generated always as identity primary key,
    match_id        text not null references public.matches(id) on delete cascade,
    competition_id  text,                         -- snapshot of the routing key
    status          text not null default 'pending',  -- pending|sending|delivered|failed
    attempts        int  not null default 0,
    last_error      text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    next_attempt_at timestamptz not null default now(),
    delivered_at    timestamptz
);

-- Coalesce while pending: at most one pending row per match. A change that
-- arrives while an older event is already 'sending'/'delivered' starts a fresh
-- pending row (so in-flight sends are never silently merged with newer state).
create unique index if not exists delivery_outbox_pending_uq
    on public.delivery_outbox (match_id) where status = 'pending';

create index if not exists delivery_outbox_drain_idx
    on public.delivery_outbox (status, next_attempt_at)
    where status = 'pending';

-- ----------------------------------------------------------------------------
-- 3) Enqueue helper + triggers (every write path feeds this one funnel)
-- ----------------------------------------------------------------------------
create or replace function public.enqueue_match_event(
    p_match_id text, p_competition_id text)
returns void
language sql
as $$
    insert into public.delivery_outbox (match_id, competition_id)
    values (p_match_id, p_competition_id)
    on conflict (match_id) where status = 'pending'
        do update set updated_at = now(),
                      -- keep a non-null routing key if a later write knows it
                      competition_id = coalesce(
                          excluded.competition_id, delivery_outbox.competition_id);
$$;

-- matches: enqueue on insert/update. Skip pure scrape-bookkeeping churn so the
-- live watcher's scraped_at heartbeats don't flood the outbox.
create or replace function public.trg_matches_enqueue()
returns trigger language plpgsql as $$
begin
    if (tg_op = 'UPDATE'
        and new.status      is not distinct from old.status
        and new.minute      is not distinct from old.minute
        and new.home_score  is not distinct from old.home_score
        and new.away_score  is not distinct from old.away_score
        and new.kickoff_at  is not distinct from old.kickoff_at
        and new.round       is not distinct from old.round
        and new.played_on   is not distinct from old.played_on
        and new.competition_id is not distinct from old.competition_id) then
        return null;  -- nothing a subscriber cares about changed
    end if;
    perform public.enqueue_match_event(new.id, new.competition_id);
    return null;
end $$;

drop trigger if exists matches_enqueue on public.matches;
create trigger matches_enqueue
    after insert or update on public.matches
    for each row execute function public.trg_matches_enqueue();

-- match_players: any stat/reporter-score change enqueues the PARENT match
-- (competition_id is looked up; coalescing collapses the whole lineup to one).
create or replace function public.trg_match_players_enqueue()
returns trigger language plpgsql as $$
declare cid text;
begin
    select competition_id into cid from public.matches where id = new.match_id;
    perform public.enqueue_match_event(new.match_id, cid);
    return null;
end $$;

drop trigger if exists match_players_enqueue on public.match_players;
create trigger match_players_enqueue
    after insert or update on public.match_players
    for each row execute function public.trg_match_players_enqueue();

-- matches_reporter_link: the raw crónica/notas blob changed -> re-send snapshot.
create or replace function public.trg_reporter_link_enqueue()
returns trigger language plpgsql as $$
declare cid text;
begin
    select competition_id into cid from public.matches where id = new.match_id;
    perform public.enqueue_match_event(new.match_id, cid);
    return null;
end $$;

drop trigger if exists reporter_link_enqueue on public.matches_reporter_link;
create trigger reporter_link_enqueue
    after insert or update on public.matches_reporter_link
    for each row execute function public.trg_reporter_link_enqueue();

-- ----------------------------------------------------------------------------
-- 4) build_match_event(): the full snapshot a subscriber receives
-- ----------------------------------------------------------------------------
-- One self-contained jsonb document: competition + match + every player who
-- played (with stats AND reporter score/MVP) + the raw reporter blob if present.
create or replace function public.build_match_event(p_match_id text)
returns jsonb
language sql
stable
as $$
    select jsonb_build_object(
        'event', 'match.update',
        'match_id', m.id,
        'sent_at', now(),
        'competition', case when c.id is null then null else jsonb_build_object(
            'id', c.id, 'name', c.name, 'slug', c.slug, 'fase', c.fase) end,
        'match', jsonb_build_object(
            'id', m.id,
            'round', m.round,
            'played_on', m.played_on,
            'url', m.url,
            'status', m.status,
            'minute', m.minute,
            'kickoff_at', m.kickoff_at,
            'home_team', case when ht.id is null then null else
                jsonb_build_object('id', ht.id, 'name', ht.name) end,
            'away_team', case when at.id is null then null else
                jsonb_build_object('id', at.id, 'name', at.name) end,
            'home_score', m.home_score,
            'away_score', m.away_score,
            'scraped_at', m.scraped_at,
            'updated_at', m.updated_at),
        'players', coalesce((
            select jsonb_agg(jsonb_build_object(
                'player_id', mp.player_id,
                'player_name', p.name,
                'team_id', mp.team_id,
                'order_index', mp.order_index,
                'shirt_number', mp.shirt_number,
                'is_captain', mp.is_captain,
                'is_starter', mp.is_starter,
                'entered_min', mp.entered_min,
                'left_min', mp.left_min,
                'goals', mp.goals,
                'assists', mp.assists,
                'yellow_cards', mp.yellow_cards,
                'red_card', mp.red_card,
                'own_goals', mp.own_goals,
                'penalties_scored', mp.penalties_scored,
                'penalties_missed', mp.penalties_missed,
                'penalties_defended', mp.penalties_defended,
                'played_under_20m', mp.played_under_20m,
                'reporter_score', mp.reporter_score,
                'reporter_is_mvp', mp.reporter_is_mvp)
                order by mp.order_index nulls last)
            from public.match_players mp
            join public.players p on p.id = mp.player_id
            where mp.match_id = m.id), '[]'::jsonb),
        'reporter', (
            select case when rl.match_id is null then null else jsonb_build_object(
                'fetched_at', rl.fetched_at,
                'format_detected', rl.format_detected,
                'urls', rl.urls,
                'home_ratings', rl.home_ratings,
                'away_ratings', rl.away_ratings) end
            from public.matches_reporter_link rl where rl.match_id = m.id)
    )
    from public.matches m
    left join public.competitions c on c.id = m.competition_id
    left join public.teams ht on ht.id = m.home_team_id
    left join public.teams at on at.id = m.away_team_id
    where m.id = p_match_id;
$$;

-- ----------------------------------------------------------------------------
-- 5) claim_outbox_batch(): atomically reserve pending rows for one dispatcher
-- ----------------------------------------------------------------------------
-- FOR UPDATE SKIP LOCKED so a Webhook firing and the pg_cron backstop never
-- send the same event twice. Returns the rows it moved to 'sending'.
create or replace function public.claim_outbox_batch(p_limit int default 50)
returns setof public.delivery_outbox
language plpgsql
as $$
begin
    return query
    with picked as (
        select id from public.delivery_outbox
        where status = 'pending' and next_attempt_at <= now()
        order by id
        for update skip locked
        limit p_limit
    )
    update public.delivery_outbox o
       set status = 'sending', attempts = o.attempts + 1, updated_at = now()
      from picked
     where o.id = picked.id
    returning o.*;
end $$;

-- ----------------------------------------------------------------------------
-- 6) RLS. The dispatcher uses service_role (bypasses RLS). Logged-in admins may
--    read both tables and manage subscribers from the web app.
-- ----------------------------------------------------------------------------
alter table public.subscribers     enable row level security;
alter table public.delivery_outbox enable row level security;

drop policy if exists subscribers_read on public.subscribers;
create policy subscribers_read on public.subscribers
    for select to authenticated using (public.is_allowed());
drop policy if exists subscribers_write on public.subscribers;
create policy subscribers_write on public.subscribers
    for all to authenticated
    using (public.is_allowed()) with check (public.is_allowed());

drop policy if exists delivery_outbox_read on public.delivery_outbox;
create policy delivery_outbox_read on public.delivery_outbox
    for select to authenticated using (public.is_allowed());

grant execute on function public.build_match_event(text)   to service_role;
grant execute on function public.claim_outbox_batch(int)    to service_role;

-- ----------------------------------------------------------------------------
-- 6b) Admin RPCs for the web app (no service key in the browser -> use these).
--     SECURITY DEFINER + is_allowed(), like reporter_link().
-- ----------------------------------------------------------------------------

-- Retry one outbox row (e.g. a 'failed' delivery). If a newer pending event for
-- the same match already supersedes it, mark this one delivered instead of
-- tripping the one-pending-per-match unique index.
create or replace function public.requeue_outbox(p_id bigint)
returns void language plpgsql security definer set search_path = public as $$
declare mid text;
begin
    if not public.is_allowed() then raise exception 'not allowed'; end if;
    select match_id into mid from public.delivery_outbox where id = p_id;
    if mid is null then return; end if;
    if exists (select 1 from public.delivery_outbox
               where match_id = mid and status = 'pending' and id <> p_id) then
        update public.delivery_outbox
           set status = 'delivered', updated_at = now() where id = p_id;
        return;
    end if;
    update public.delivery_outbox
       set status = 'pending', attempts = 0, next_attempt_at = now(),
           last_error = null, delivered_at = null, updated_at = now()
     where id = p_id;
end $$;

-- Enqueue a fresh snapshot for every match in a league (NULL = all leagues).
-- Use this to backfill a newly-added subscriber. Returns rows enqueued.
create or replace function public.replay_competition(p_competition_id text)
returns int language plpgsql security definer set search_path = public as $$
declare n int;
begin
    if not public.is_allowed() then raise exception 'not allowed'; end if;
    insert into public.delivery_outbox (match_id, competition_id)
    select m.id, m.competition_id from public.matches m
     where p_competition_id is null or m.competition_id = p_competition_id
    on conflict (match_id) where status = 'pending' do nothing;
    get diagnostics n = row_count;
    return n;
end $$;

grant execute on function public.requeue_outbox(bigint)      to authenticated;
grant execute on function public.replay_competition(text)    to authenticated;

-- ----------------------------------------------------------------------------
-- 7) Wiring the dispatcher (run AFTER deploying the `dispatch` Edge Function)
-- ----------------------------------------------------------------------------
-- Replace <PROJECT_REF> and <SERVICE_ROLE_KEY> below. Requires the pg_net and
-- pg_cron extensions (enable them under Database -> Extensions first).
--
--   create extension if not exists pg_net;
--   create extension if not exists pg_cron;
--
-- (a) Low-latency trigger: ping the dispatcher whenever a row becomes pending.
--     (Prefer setting this up as a Database Webhook in the dashboard on
--      delivery_outbox INSERT; this trigger is the SQL-only equivalent.)
--
--   create or replace function public.ping_dispatch()
--   returns trigger language plpgsql as $$
--   begin
--     perform net.http_post(
--       url     := 'https://<PROJECT_REF>.functions.supabase.co/dispatch',
--       headers := jsonb_build_object(
--                    'Content-Type', 'application/json',
--                    'Authorization', 'Bearer <SERVICE_ROLE_KEY>'),
--       body    := '{}'::jsonb);
--     return null;
--   end $$;
--   drop trigger if exists outbox_ping on public.delivery_outbox;
--   create trigger outbox_ping after insert on public.delivery_outbox
--     for each row when (new.status = 'pending')
--     execute function public.ping_dispatch();
--
-- (b) Retry backstop: drain anything still pending every 30 seconds.
--
--   select cron.schedule('dispatch-drain', '30 seconds', $cron$
--     select net.http_post(
--       url     := 'https://<PROJECT_REF>.functions.supabase.co/dispatch',
--       headers := jsonb_build_object(
--                    'Content-Type', 'application/json',
--                    'Authorization', 'Bearer <SERVICE_ROLE_KEY>'),
--       body    := '{}'::jsonb);
--   $cron$);
