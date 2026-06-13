-- Migration: DB-backed job queue for parallel crawler workers.
-- Run once in the Supabase SQL editor. Safe to re-run (additive + idempotent).
--
-- Replaces the old "one self-hosted runner, dispatch-assigns-work" model with a
-- claimable queue so several workers (one per registered self-hosted runner) can
-- pick up DIFFERENT jobs at the same time -- e.g. the live-watch daemon on one
-- worker while a backfill/reporter/squad sync runs on another.
--
-- How it stays correct:
--   * claim_job() selects the next pending job with  FOR UPDATE SKIP LOCKED  and
--     flips it to 'running' in one transaction. Two workers therefore NEVER grab
--     the same row: the second skips the locked one and takes the next.
--   * a partial unique index on dedupe_key (active rows only) stops the same
--     target being queued twice while one is already in flight -- the collision
--     safety the single-runner FIFO used to provide implicitly (see crawl.yml).
--   * each running job carries a lease (lease_expires_at). Workers heartbeat to
--     extend it; reclaim_jobs() (called by a pg_cron backstop) returns jobs whose
--     lease expired -- i.e. whose worker died -- back to 'pending'.
--
-- crawl_runs is unchanged: it remains the per-execution LOG. jobs is the QUEUE.
-- A job optionally links to the crawl_runs row its execution produced (run_id).

create table if not exists public.jobs (
    id               bigint generated always as identity primary key,
    kind             text not null,
        -- round | match | backfill | scheduled | reporter | reporter_sweep |
        -- watch | teams | roster | players | comp_full | comp_players
    params           jsonb not null default '{}'::jsonb,   -- per-kind inputs
    dedupe_key       text,                  -- active-uniqueness key; null = never deduped
    priority         int  not null default 100,            -- lower = sooner
    status           text not null default 'pending',
        -- pending | running | done | error | canceled
    claimed_by       text,                  -- worker id currently holding the lease
    lease_expires_at timestamptz,           -- running job is dead if now() > this
    attempts         int  not null default 0,
    max_attempts     int  not null default 3,
    last_error       text,
    run_id           bigint,                -- crawl_runs row this execution logged to
    created_at       timestamptz not null default now(),
    started_at       timestamptz,
    finished_at      timestamptz
);

-- Fast "next claimable job" scan.
create index if not exists jobs_claim_idx
    on public.jobs (priority, created_at)
    where status = 'pending';

create index if not exists jobs_status_idx
    on public.jobs (status, created_at desc);

-- At most ONE active (pending or running) job per dedupe_key. enqueue_job()
-- relies on this to make a duplicate enqueue a no-op.
create unique index if not exists jobs_active_dedupe_idx
    on public.jobs (dedupe_key)
    where dedupe_key is not null and status in ('pending', 'running');

-- ----------------------------------------------------------------------------
-- RPCs (security definer so the website can enqueue under RLS; workers call the
-- claim/heartbeat/finish/reclaim ones with the service_role key, which bypasses
-- RLS anyway).
-- ----------------------------------------------------------------------------

-- Atomically grab the next pending job and mark it running under this worker's
-- lease. Returns the claimed row, or NULL when the queue is empty (for the
-- requested kinds). p_kinds = NULL means "any kind".
create or replace function public.claim_job(
    p_worker        text,
    p_lease_seconds int     default 120,
    p_kinds         text[]  default null)
returns public.jobs
language plpgsql
security definer
set search_path = public
as $$
declare
    j public.jobs;
begin
    select * into j
      from public.jobs
     where status = 'pending'
       and (p_kinds is null or kind = any (p_kinds))
     order by priority, created_at
     for update skip locked
     limit 1;

    if not found then
        return null;
    end if;

    update public.jobs
       set status           = 'running',
           claimed_by       = p_worker,
           attempts         = attempts + 1,
           started_at       = coalesce(started_at, now()),
           lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = j.id
     returning * into j;

    return j;
end;
$$;

-- Extend this worker's lease on a running job. Returns false if the lease was
-- lost (job reclaimed / no longer ours) -- the worker should then stop.
create or replace function public.heartbeat_job(
    p_id            bigint,
    p_worker        text,
    p_lease_seconds int default 120)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    n int;
begin
    update public.jobs
       set lease_expires_at = now() + make_interval(secs => p_lease_seconds)
     where id = p_id and claimed_by = p_worker and status = 'running';
    get diagnostics n = row_count;
    return n > 0;
end;
$$;

-- Terminal-state a job. On 'error' with attempts left, the job goes back to
-- 'pending' for another worker to retry; once max_attempts is reached it stays
-- 'error'. Any other status ('done', 'canceled') is final.
create or replace function public.finish_job(
    p_id     bigint,
    p_status text,
    p_error  text default null)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_status = 'error' then
        update public.jobs
           set status      = case when attempts >= max_attempts
                                  then 'error' else 'pending' end,
               last_error  = p_error,
               finished_at = case when attempts >= max_attempts
                                  then now() else null end,
               claimed_by       = null,
               lease_expires_at = null
         where id = p_id;
    else
        update public.jobs
           set status      = p_status,
               last_error  = p_error,
               finished_at = now(),
               claimed_by       = null,
               lease_expires_at = null
         where id = p_id;
    end if;
end;
$$;

-- Return jobs whose lease expired (worker crashed) to the queue, failing those
-- out of retries. Call from a pg_cron backstop, e.g. every minute:
--   select cron.schedule('reclaim-jobs', '* * * * *', $$select public.reclaim_jobs()$$);
create or replace function public.reclaim_jobs()
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
    n int;
begin
    update public.jobs
       set status      = case when attempts >= max_attempts
                              then 'error' else 'pending' end,
           last_error  = coalesce(last_error, 'lease expired (worker died)'),
           finished_at = case when attempts >= max_attempts
                              then now() else null end,
           claimed_by       = null,
           lease_expires_at = null
     where status = 'running' and lease_expires_at < now();
    get diagnostics n = row_count;
    return n;
end;
$$;

-- Enqueue a job, deduped: if an active job with the same dedupe_key already
-- exists, this is a no-op and returns that existing job's id. Returns the new
-- (or existing) job id.
create or replace function public.enqueue_job(
    p_kind       text,
    p_params     jsonb default '{}'::jsonb,
    p_dedupe_key text  default null,
    p_priority   int   default 100)
returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare
    new_id bigint;
begin
    insert into public.jobs (kind, params, dedupe_key, priority)
    values (p_kind, coalesce(p_params, '{}'::jsonb), p_dedupe_key, p_priority)
    on conflict (dedupe_key)
        where dedupe_key is not null and status in ('pending', 'running')
        do nothing
    returning id into new_id;

    if new_id is null then  -- a matching active job already exists
        select id into new_id
          from public.jobs
         where dedupe_key = p_dedupe_key
           and status in ('pending', 'running')
         order by created_at
         limit 1;
    end if;
    return new_id;
end;
$$;

-- ----------------------------------------------------------------------------
-- RLS: approved users may read the queue + enqueue (via the RPC). Workers use
-- the service_role key and bypass RLS.
-- ----------------------------------------------------------------------------

alter table public.jobs enable row level security;

drop policy if exists jobs_read on public.jobs;
create policy jobs_read on public.jobs
    for select to authenticated
    using (public.is_allowed());

drop policy if exists jobs_insert on public.jobs;
create policy jobs_insert on public.jobs
    for insert to authenticated
    with check (public.is_allowed());

grant execute on function public.enqueue_job(text, jsonb, text, int)
    to authenticated;
