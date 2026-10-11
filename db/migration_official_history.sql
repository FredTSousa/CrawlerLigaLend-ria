-- Migration: official-stats history + the two hourly follow-up sweeps.
-- Run once in the Supabase SQL editor, AFTER db/migration_official_stats.sql,
-- db/migration_job_queue.sql and db/migration_cron_queue.sql (needs
-- enqueue_job / dispatch_worker). Safe to re-run (idempotent).
--
-- 1) Official history. Before switching the league to the official site as
--    its stats source we need to know whether its values are final at the
--    whistle. ligaportugal.py now records every value that changes between
--    two fetches of the same match in official_stat_history (the first fetch
--    is the baseline: official_first_synced_at). The hourly 'official_sweep'
--    fetches each finished match soon after the end, re-checks it hourly until
--    the site marks it official, and once more a day after kickoff.
--
-- 2) zerozero settle recrawl. zerozero corrects stats hours after a match
--    (round 8: missed yellows, a sub minute, assists all appeared later), but
--    nothing re-crawled a match once final. The hourly 'settle_sweep'
--    (sync.py run_settle_sweep) re-crawls every finished match once, ~14 h
--    after kickoff, and stamps matches.settled_crawl_at.

alter table public.matches
    add column if not exists official_first_synced_at timestamptz,
    add column if not exists settled_crawl_at         timestamptz;

update public.matches
   set official_first_synced_at = official_synced_at
 where official_first_synced_at is null and official_synced_at is not null;

create table if not exists public.official_stat_history (
    id             bigint generated always as identity primary key,
    match_id       text not null references public.matches(id) on delete cascade,
    lp_player_id   text not null,
    player_id      text references public.players(id),
    lp_name        text,
    stat           text not null,  -- goals|own_goals|yellow_cards|red_card|entered_min|left_min|assists
    old_value      int,            -- null = not there before (row/assist first seen)
    new_value      int,            -- null = gone now
    official_final boolean,        -- site's hasOfficialResult at the time
    observed_at    timestamptz not null default now()
);

create index if not exists official_stat_history_match_idx
    on public.official_stat_history (match_id);

alter table public.official_stat_history enable row level security;

drop policy if exists official_stat_history_read on public.official_stat_history;
create policy official_stat_history_read on public.official_stat_history
    for select to authenticated using (public.is_allowed());

-- ----------------------------------------------------------------------------
-- Hourly sweeps through the job queue (same pattern as enqueue_dates_sweep).
-- ----------------------------------------------------------------------------
create or replace function public.enqueue_official_sweep()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    perform public.enqueue_job('official_sweep', '{}'::jsonb, 'official_sweep', 85);
    perform public.dispatch_worker();
end;
$$;

create or replace function public.enqueue_settle_sweep()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    perform public.enqueue_job('settle_sweep', '{}'::jsonb, 'settle_sweep', 92);
    perform public.dispatch_worker();
end;
$$;

grant execute on function public.enqueue_official_sweep() to service_role;
grant execute on function public.enqueue_settle_sweep() to service_role;

do $$ begin
    perform cron.unschedule('enqueue-official-sweep');
exception when others then null; end $$;
do $$ begin
    perform cron.unschedule('enqueue-settle-sweep');
exception when others then null; end $$;

-- Offset from the 6-hourly sweep (:00) and each other.
select cron.schedule('enqueue-official-sweep', '20 * * * *',
    $$select public.enqueue_official_sweep();$$);
select cron.schedule('enqueue-settle-sweep', '40 * * * *',
    $$select public.enqueue_settle_sweep();$$);
