-- Migration: daily "fixture dates only" sweep.
--
-- Companion to the 6-hourly full sweep (migration_cron_queue.sql's
-- enqueue_scheduled_sweep -> 'scheduled' job -> sync.py run_scheduled, which
-- visits every match page it decides needs a re-crawl). This adds a cheaper,
-- more frequent pass that ONLY refreshes played_on/kickoff_at: once a day,
-- for the current round + next 2 rounds of every active competition
-- (crawl_active=true). Each round costs a single HTTP request -- the round
-- table alone (crawler.get_fixture), no per-match page visits -- via
-- sync.py's write_match_dates, so it never touches score/status/lineups and
-- is safe to run across every league daily. It exists to catch postponements
-- and newly-published kickoff times well before the 6-hourly sweep would.
--
-- Same enqueue-through-the-job-queue pattern as enqueue_scheduled_sweep /
-- dispatch_reporter_sweep: enqueue a deduped 'dates_sweep' job, then wake a
-- worker (worker.py picks it up and runs sync.py's run_dates_sweep()).
--
-- Run once in the Supabase SQL editor, AFTER db/migration_job_queue.sql and
-- db/migration_cron_queue.sql (needs enqueue_job/dispatch_worker). Safe to
-- re-run (idempotent).

create or replace function public.enqueue_dates_sweep()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    perform public.enqueue_job('dates_sweep', '{}'::jsonb, 'dates_sweep', 95);
    perform public.dispatch_worker();
end;
$$;

grant execute on function public.enqueue_dates_sweep() to service_role;

-- Daily at 06:00 UTC -- ahead of the morning news cycle, well before the
-- 6-hourly sweep's next tick (idempotent: drop any prior copy first).
do $$ begin
    perform cron.unschedule('enqueue-dates-sweep');
exception when others then null; end $$;

select cron.schedule('enqueue-dates-sweep', '0 6 * * *',
    $$select public.enqueue_dates_sweep();$$);
