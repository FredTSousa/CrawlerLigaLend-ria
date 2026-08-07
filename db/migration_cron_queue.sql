-- Migration: route the scheduled crons through the job queue.
--
-- Builds on db/migration_job_queue.sql. Previously each scheduler dispatched a
-- specific GitHub workflow (crawl.yml schedule / dispatch_reporter_sweep ->
-- reporter_sweep.yml / start_due_live_watches -> watch.yml). Now each one
-- ENQUEUES a job and wakes a generic worker, so the crons share the same
-- claim-based queue + parallelism as on-demand work:
--
--   crawl.yml 6-hourly schedule  ->  enqueue_job('scheduled')      (this file)
--   dispatch_reporter_sweep()    ->  enqueue_job('reporter_sweep')
--   start_due_live_watches()     ->  enqueue_job('watch')          (singleton)
--
-- A worker is woken by dispatch_worker(), which POSTs a workflow_dispatch to
-- worker.yml — rate-limited and only when jobs are actually pending, so the
-- per-minute tick doesn't spam GitHub. reclaim_jobs() runs each minute too, so a
-- crashed worker's job is retried.
--
-- After this, the GitHub `schedule:` trigger is removed from crawl.yml; the old
-- per-kind workflows (crawl/reporter_sweep/watch.yml) stay as manual fallbacks.
--
-- Run once in the Supabase SQL editor, AFTER db/migration_job_queue.sql. Safe to
-- re-run (idempotent). Uses the same pg_cron + pg_net + Vault secrets
-- (gh_dispatch_token, gh_repo) the existing dispatchers already rely on.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Single-row state for rate-limiting worker wakes.
create table if not exists public.queue_state (
    id                      boolean primary key default true,
    last_worker_dispatch_at timestamptz,
    constraint queue_state_singleton check (id)
);
insert into public.queue_state (id) values (true) on conflict (id) do nothing;

-- Wake a generic worker (worker.yml) when there is pending work. Rate-limited to
-- ~once a minute so the per-minute tick + on-demand enqueues don't pile up GitHub
-- runs; GitHub assigns the run to an idle self-hosted runner (or queues it).
create or replace function public.dispatch_worker()
returns void
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    v_pending int;
    v_running int;
    v_last    timestamptz;
    v_token   text;
    v_repo    text;
begin
    select count(*) into v_pending from public.jobs where status = 'pending';
    if v_pending = 0 then
        return;  -- nothing waiting; a running worker will claim future jobs
    end if;

    -- Active workers drain the queue in a loop; no need to spawn another one
    -- until they all die or their leases expire (reclaim_jobs handles that).
    select count(*) into v_running
      from public.jobs
     where status = 'running' and lease_expires_at > now();
    if v_running > 0 then
        return;
    end if;

    select last_worker_dispatch_at into v_last from public.queue_state where id;
    if v_last is not null and v_last > now() - interval '55 seconds' then
        return;  -- a wake was just sent; let it land before sending another
    end if;

    select decrypted_secret into v_token
      from vault.decrypted_secrets where name = 'gh_dispatch_token';
    select decrypted_secret into v_repo
      from vault.decrypted_secrets where name = 'gh_repo';
    if v_token is null or v_repo is null then
        raise warning 'dispatch_worker: missing gh_dispatch_token / gh_repo in Vault; worker not woken';
        return;
    end if;

    perform net.http_post(
        url := format(
            'https://api.github.com/repos/%s/actions/workflows/worker.yml/dispatches',
            v_repo),
        headers := jsonb_build_object(
            'Authorization',        'Bearer ' || v_token,
            'Accept',               'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'Content-Type',         'application/json',
            'User-Agent',           'crawler-llp-worker-waker'),
        body := jsonb_build_object('ref', 'main')
    );

    update public.queue_state set last_worker_dispatch_at = now() where id;
end;
$$;

-- 6-hourly league sweep: replaces crawl.yml's GitHub `schedule:` trigger. Deduped
-- so a still-running sweep isn't queued twice.
create or replace function public.enqueue_scheduled_sweep()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    perform public.enqueue_job('scheduled', '{}'::jsonb, 'scheduled:sweep', 90);
    perform public.dispatch_worker();
end;
$$;

-- 30-minute reporter sweep: same cron entry as before (dispatch-reporter-sweep),
-- only the body changes — it now enqueues instead of dispatching reporter_sweep.yml.
create or replace function public.dispatch_reporter_sweep()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    perform public.enqueue_job('reporter_sweep', '{}'::jsonb, 'reporter_sweep', 95);
    perform public.dispatch_worker();
end;
$$;

-- Per-minute kickoff watcher: promote due matches, then ensure the singleton
-- live-watch daemon is queued. The dedupe_key 'watch:daemon' guarantees only one
-- active watch job, and reclaim_jobs() restarts it if its worker dies — so the
-- bespoke crawl_runs-heartbeat liveness logic the old version needed is replaced
-- by the queue's own singleton + lease. A running daemon already absorbs newly
-- flagged matches via its ~30s DB poll, so the deduped enqueue is a harmless no-op
-- while one is alive.
--
-- lead = 45 minutes before kickoff (raised from 5 min so the pre-kickoff poll
--   loop has real odds of catching the starting XI once zerozero publishes it,
--   instead of only seeding the lineup 5 minutes before the whistle).
-- grace = 90 minutes after kickoff (catches a match the cron missed).
create or replace function public.start_due_live_watches()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_promoted integer;
    v_needs    boolean;
begin
    update public.matches
       set watch      = true,
           updated_at = now()
     where status = 'scheduled'
       and coalesce(watch, false) = false
       and kickoff_at is not null
       and kickoff_at <= now() + interval '45 minutes'
       and kickoff_at >= now() - interval '90 minutes';
    get diagnostics v_promoted = row_count;

    select exists(
        select 1 from public.matches where watch = true and status <> 'final'
    ) into v_needs;

    if v_needs then
        perform public.enqueue_job('watch', '{}'::jsonb, 'watch:daemon', 10);
        perform public.dispatch_worker();
    end if;

    return v_promoted;
end;
$$;

grant execute on function public.dispatch_worker()          to service_role;
grant execute on function public.enqueue_scheduled_sweep()  to service_role;
grant execute on function public.dispatch_reporter_sweep()  to service_role;
grant execute on function public.start_due_live_watches()   to service_role;

-- ----------------------------------------------------------------------------
-- Cron schedules (idempotent: unschedule any prior copy first).
-- dispatch-reporter-sweep and start-due-live-watches already exist from earlier
-- migrations and keep their cadence; we (re)assert them so this file is
-- self-contained. Only their function bodies changed above.
-- ----------------------------------------------------------------------------

do $$ begin perform cron.unschedule('queue-tick');             exception when others then null; end $$;
do $$ begin perform cron.unschedule('enqueue-scheduled-sweep'); exception when others then null; end $$;
do $$ begin perform cron.unschedule('dispatch-reporter-sweep'); exception when others then null; end $$;
do $$ begin perform cron.unschedule('start-due-live-watches');  exception when others then null; end $$;

-- Reclaim dead jobs + wake a worker when work is pending — every minute.
select cron.schedule('queue-tick', '* * * * *',
    $$select public.reclaim_jobs(); select public.dispatch_worker();$$);

-- 6-hourly league sweep (was crawl.yml's GitHub schedule).
select cron.schedule('enqueue-scheduled-sweep', '0 */6 * * *',
    $$select public.enqueue_scheduled_sweep();$$);

-- 30-minute reporter gap-fill sweep.
select cron.schedule('dispatch-reporter-sweep', '*/30 * * * *',
    $$select public.dispatch_reporter_sweep();$$);

-- Per-minute kickoff watcher.
select cron.schedule('start-due-live-watches', '* * * * *',
    $$select public.start_due_live_watches();$$);
