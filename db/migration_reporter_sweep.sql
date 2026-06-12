-- Migration: drive the 30-minute reporter sweep from Supabase pg_cron.
--
-- The reporter sweep (reporter_sweep.yml -> sync.py --reporter-sweep) fetches
-- A Bola / Goal.com ratings for any FINAL match that still lacks them, across
-- active reporter-covered competitions. It must run on the residential-IP
-- self-hosted runner, so Supabase dispatches the GitHub workflow rather than
-- doing the work itself -- the same pattern as the kickoff/watch dispatcher.
--
-- This adds a pg_cron job (every 30 min) that POSTs a workflow_dispatch to
-- reporter_sweep.yml. The workflow no longer carries a GitHub `schedule:`
-- trigger; this is its only scheduler. Duplicate/extra dispatches are harmless:
-- the sweep only fills matches that are still missing scores (idempotent), and
-- the single self-hosted runner serializes runs FIFO.
--
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).
--
-- Requires pg_cron + pg_net (already enabled for the subscriptions dispatcher,
-- the emulation tick and the kickoff-watch cron) and the same two Vault secrets
-- the kickoff-watch dispatcher uses -- set once if not already present:
--
--   select vault.create_secret('ghp_xxxxxxxx',        'gh_dispatch_token');
--   select vault.create_secret('FredTSousa/CrawlerLigaLend-ria', 'gh_repo');
--
-- The token needs the `workflow` scope (same token the website uses as
-- GH_DISPATCH_TOKEN). To rotate: select vault.update_secret((select id from
-- vault.secrets where name='gh_dispatch_token'), 'ghp_new').

create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function public.dispatch_reporter_sweep()
returns void
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    v_token text;
    v_repo  text;
begin
    select decrypted_secret into v_token
      from vault.decrypted_secrets where name = 'gh_dispatch_token';
    select decrypted_secret into v_repo
      from vault.decrypted_secrets where name = 'gh_repo';

    if v_token is null or v_repo is null then
        raise warning 'dispatch_reporter_sweep: missing gh_dispatch_token / gh_repo in Vault; reporter sweep not dispatched';
        return;
    end if;

    perform net.http_post(
        url := format(
            'https://api.github.com/repos/%s/actions/workflows/reporter_sweep.yml/dispatches',
            v_repo),
        headers := jsonb_build_object(
            'Authorization',        'Bearer ' || v_token,
            'Accept',               'application/vnd.github+json',
            'X-GitHub-Api-Version', '2022-11-28',
            'Content-Type',         'application/json',
            'User-Agent',           'crawler-llp-reporter-sweep-cron'),
        body := jsonb_build_object('ref', 'main')
    );
end;
$$;

grant execute on function public.dispatch_reporter_sweep() to service_role;

-- Schedule it every 30 minutes (idempotent: drop any prior copy first).
do $$ begin
    perform cron.unschedule('dispatch-reporter-sweep');
exception when others then null; end $$;

select cron.schedule(
    'dispatch-reporter-sweep',
    '*/30 * * * *',
    $$select public.dispatch_reporter_sweep();$$
);
