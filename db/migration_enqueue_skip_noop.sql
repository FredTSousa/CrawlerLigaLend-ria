-- =============================================================================
-- Skip no-op re-saves in the delivery_outbox enqueue triggers.
--
-- trg_matches_enqueue (migration_subscriptions.sql) already ignores updates
-- where nothing a subscriber reads changed. trg_match_players_enqueue and
-- trg_reporter_link_enqueue did not: every upsert re-queued the parent match,
-- even when the crawler re-wrote identical values and only the bookkeeping
-- timestamp moved. The A Bola reporter sweep (dispatch-reporter-sweep, every
-- 30 min) and the zerozero re-crawls re-upsert whole lineups, so each pass sent
-- an unchanged snapshot to every subscriber. On Liga Lendária that was ~42% of
-- all /api/crawler/ingest calls, almost all between 00:00 and 04:00 UTC
-- (Vercel Observability, 30 days to 2026-11-12) — each one a paid serverless
-- invocation doing ~25 database reads for nothing.
--
-- Now an UPDATE only enqueues when some column other than the bookkeeping
-- timestamp (match_players.updated_at / matches_reporter_link.fetched_at)
-- actually changed. INSERTs always enqueue, exactly as before. Comparing the
-- whole row as jsonb means any column added later counts as a real change
-- automatically — only add a key to the exclusion if it is pure bookkeeping.
--
-- Idempotent: safe to re-run. Only the trigger functions are replaced; the
-- triggers themselves (and their names) are unchanged.
-- =============================================================================

create or replace function public.trg_match_players_enqueue()
returns trigger language plpgsql as $$
declare cid text;
begin
    if tg_op = 'UPDATE'
       and (to_jsonb(new) - 'updated_at') is not distinct from (to_jsonb(old) - 'updated_at') then
        return null;  -- re-saved with identical values: nothing to deliver
    end if;
    select competition_id into cid from public.matches where id = new.match_id;
    perform public.enqueue_match_event(new.match_id, cid);
    return null;
end $$;

create or replace function public.trg_reporter_link_enqueue()
returns trigger language plpgsql as $$
declare cid text;
begin
    if tg_op = 'UPDATE'
       and (to_jsonb(new) - 'fetched_at') is not distinct from (to_jsonb(old) - 'fetched_at') then
        return null;  -- sweep re-fetched the same ratings: nothing to deliver
    end if;
    select competition_id into cid from public.matches where id = new.match_id;
    perform public.enqueue_match_event(new.match_id, cid);
    return null;
end $$;
