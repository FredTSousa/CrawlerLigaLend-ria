-- Migration: auto-start a live watch when a match reaches its kickoff_at.
--
-- The live pipeline already exists: a match flagged watch=true is followed by
-- live_watch.py --daemon (run via the watch.yml GitHub workflow on the
-- residential-IP runner). The daemon re-reads the watch list every ~30s, so it
-- absorbs newly-flagged matches on its own — we only have to (1) flip the flag
-- at kickoff and (2) start the daemon when nothing is currently being watched.
--
-- This adds a pg_cron job (every minute) that does exactly that:
--   1) promote scheduled matches whose kickoff is imminent / just passed to
--      watch=true;
--   2) if that promotion is the *leading edge* of a watch session (nothing was
--      being watched before), dispatch watch.yml to bring the daemon up.
--
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).
--
-- Requires pg_cron + pg_net (already enabled for the subscriptions dispatcher
-- and the emulation tick) and two secrets in Supabase Vault — set these once:
--
--   select vault.create_secret('ghp_xxxxxxxx',        'gh_dispatch_token');
--   select vault.create_secret('FredTSousa/CrawlerLigaLend-ria', 'gh_repo');
--
-- The token needs the `workflow` scope (same token the website uses as
-- GH_DISPATCH_TOKEN). To rotate: select vault.update_secret((select id from
-- vault.secrets where name='gh_dispatch_token'), 'ghp_new').

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- How early before kickoff to bring the watcher up (lets it seed lineups before
-- the whistle), and how long after kickoff we keep trying to catch a match the
-- cron may have missed. A still-"scheduled" match outside this window is treated
-- as stale and left alone rather than resurrected.
--   lead  = 5 minutes before kickoff
--   grace = 15 minutes after kickoff

create or replace function public.start_due_live_watches()
returns integer
language plpgsql
security definer
set search_path = public, vault
as $$
declare
    v_active_before integer;
    v_promoted      integer;
    v_token         text;
    v_repo          text;
begin
    -- Is a watch session already in flight? If so, the running daemon will pick
    -- up anything we newly flag on its next ~30s loop and we must NOT dispatch
    -- again (watch.yml is cancel-in-progress; a second dispatch would needlessly
    -- kill and restart the live watcher).
    select count(*) into v_active_before
      from public.matches
     where watch = true
       and status <> 'final';

    -- 1) Promote scheduled matches whose kickoff window has arrived.
    update public.matches
       set watch      = true,
           updated_at = now()
     where status = 'scheduled'
       and coalesce(watch, false) = false
       and kickoff_at is not null
       and kickoff_at <= now() + interval '5 minutes'
       and kickoff_at >= now() - interval '15 minutes';
    get diagnostics v_promoted = row_count;

    -- 2) Leading edge only: start the daemon when work just appeared and none
    --    was running. Mid-session matches ride the running daemon's poll loop.
    if v_promoted > 0 and v_active_before = 0 then
        select decrypted_secret into v_token
          from vault.decrypted_secrets where name = 'gh_dispatch_token';
        select decrypted_secret into v_repo
          from vault.decrypted_secrets where name = 'gh_repo';

        if v_token is null or v_repo is null then
            raise warning 'start_due_live_watches: missing gh_dispatch_token / gh_repo in Vault; % match(es) flagged but daemon not dispatched', v_promoted;
        else
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
                body := jsonb_build_object('ref', 'main')
            );
        end if;
    end if;

    return v_promoted;
end;
$$;

grant execute on function public.start_due_live_watches() to service_role;

-- Schedule it every minute (idempotent: drop any prior copy first).
do $$ begin
    perform cron.unschedule('start-due-live-watches');
exception when others then null; end $$;

select cron.schedule(
    'start-due-live-watches',
    '* * * * *',
    $$select public.start_due_live_watches();$$
);
