-- Migration: make the kickoff live-watch dispatcher self-healing.
--
-- Supersedes the dispatch logic in migration_kickoff_watch.sql. That version
-- gated dispatch on `v_active_before = 0`, i.e. "no match is currently flagged
-- watch=true". The flaw: a flag is not a running daemon. Any match left
-- watch=true / status<>final with no live watcher (failed dispatch, crashed or
-- idle-exited daemon, runner offline) made that count >0 forever and silently
-- suppressed every future dispatch — a permanent deadlock.
--
-- The fix decides on *actual daemon liveness*, not flag state:
--   * the watcher heartbeats public.crawl_runs.last_seen_at every loop
--     (live_watch.py), so a stale/absent heartbeat means no watcher is alive;
--   * the cron dispatches whenever there is something to watch AND no watcher
--     is alive — which now also recovers a missed dispatch, a dead daemon, and
--     a match stranded past its kickoff grace window (a built-in reconciler);
--   * each dispatch records a 'queued' crawl_runs row the daemon adopts via
--     run_id, so the ~1-2 min runner-startup gap (before the first heartbeat)
--     doesn't look like "no watcher" and trigger a cancel/restart loop;
--   * watch runs that never check in are expired to 'error' instead of
--     vanishing, so a broken dispatch is visible rather than silent.
--
-- Run once in the Supabase SQL editor, AFTER deploying the matching live_watch.py
-- + sync.py (which write last_seen_at). Safe to re-run (idempotent). The pg_cron
-- job from migration_kickoff_watch.sql still calls start_due_live_watches(); only
-- the function body changes, so no re-scheduling is needed.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Heartbeat column the watcher bumps each loop. coalesce() fallbacks below keep
-- the function correct for legacy rows written before this column existed.
alter table public.crawl_runs
    add column if not exists last_seen_at timestamptz;

-- How long without a heartbeat before a watcher is presumed dead. The daemon
-- beats every ~30s (light_interval), so 3 minutes = 6 missed beats: comfortably
-- past any single heavy full-crawl, well short of letting a dead run linger.
create or replace function public.start_due_live_watches()
returns integer
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    v_promoted integer;
    v_needs    boolean;
    v_live     boolean;
    v_run_id   bigint;
    v_token    text;
    v_repo     text;
begin
    -- 1) Promote scheduled matches whose kickoff window has arrived
    --    (lead 5 min before, grace 15 min after). Unchanged from before.
    update public.matches
       set watch      = true,
           updated_at = now()
     where status = 'scheduled'
       and coalesce(watch, false) = false
       and kickoff_at is not null
       and kickoff_at <= now() + interval '5 minutes'
       and kickoff_at >= now() - interval '90 minutes';
    get diagnostics v_promoted = row_count;

    -- 2) Anything still needing a watcher? If not, we're done.
    select exists(
        select 1 from public.matches
         where watch = true and status <> 'final'
    ) into v_needs;

    if not v_needs then
        return v_promoted;
    end if;

    -- 3) Expire watch runs that were dispatched but never reported in (runner
    --    offline, crash before first heartbeat). This stops them counting as a
    --    live watcher AND surfaces the failure instead of swallowing it.
    update public.crawl_runs
       set status      = 'error',
           error       = 'watch daemon never reported in (no heartbeat within 3m)',
           finished_at = now()
     where kind = 'watch'
       and status in ('queued', 'running')
       and coalesce(last_seen_at, started_at, created_at) < now() - interval '3 minutes';

    -- 4) Is a watcher actually alive (recent heartbeat) or just coming up
    --    (freshly queued)? Either way a watcher is on the job and re-reads the
    --    watch list every ~30s, so it will absorb whatever we just flagged.
    --    Dispatching again would only cancel it (watch.yml is cancel-in-progress).
    select exists(
        select 1 from public.crawl_runs
         where kind = 'watch'
           and status in ('queued', 'running')
           and coalesce(last_seen_at, started_at, created_at) > now() - interval '3 minutes'
    ) into v_live;

    if v_live then
        return v_promoted;
    end if;

    -- 5) Work exists and nothing is alive -> (re)start the daemon. Covers the
    --    leading edge (new kickoff), a crashed/exited daemon, a rejected or lost
    --    dispatch, and a match stuck past its grace window.
    select decrypted_secret into v_token
      from vault.decrypted_secrets where name = 'gh_dispatch_token';
    select decrypted_secret into v_repo
      from vault.decrypted_secrets where name = 'gh_repo';

    if v_token is null or v_repo is null then
        raise warning 'start_due_live_watches: missing gh_dispatch_token / gh_repo in Vault; watch needed but daemon not dispatched';
        return v_promoted;
    end if;

    -- Record the dispatch as a queued run the daemon adopts (run_id), so the
    -- next tick sees "a watcher is coming up" during runner startup and does not
    -- re-dispatch (which would cancel the booting daemon).
    insert into public.crawl_runs (trigger, kind, target, status, last_seen_at, created_at)
         values ('cron', 'watch', 'watch-list', 'queued', now(), now())
      returning id into v_run_id;

    perform net.http_post(
        url := format(
            'https://api.github.com/repos/%s/actions/workflows/watch.yml/dispatches',
            v_repo),
        headers := jsonb_build_object(
            'Authorization',        'Bearer ' || v_token,
            'Accept',               'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'Content-Type',         'application/json',
            'User-Agent',           'crawler-llp-kickoff-cron'),
        -- 'match' is intentionally omitted (the job runs --daemon); it must be
        -- an optional input in watch.yml or GitHub rejects this with HTTP 422.
        body := jsonb_build_object(
            'ref',    'main',
            'inputs', jsonb_build_object('run_id', v_run_id::text))
    );

    return v_promoted;
end;
$$;

grant execute on function public.start_due_live_watches() to service_role;
