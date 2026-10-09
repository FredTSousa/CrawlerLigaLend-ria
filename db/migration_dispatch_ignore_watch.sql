-- dispatch_worker(): don't let a running live-watch job block other workers.
--
-- The watch job holds its worker for hours (the daemon loops until every match
-- is final) and never claims other jobs, so counting it as an "active worker"
-- meant pending jobs (reporter_sweep, scheduled, ...) sat in the queue for the
-- whole match while a second runner idled. Exclude kind='watch' from the check.
--
-- Run in the Supabase SQL editor. Idempotent.

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
     where status = 'running' and lease_expires_at > now()
       and kind <> 'watch';  -- the live-watch daemon never drains the queue
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
