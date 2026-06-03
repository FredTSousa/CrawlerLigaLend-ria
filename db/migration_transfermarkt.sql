-- Migration: Transfermarkt club mapping on teams.
-- Run once in the Supabase SQL editor. Safe to re-run (additive + idempotent).
--
-- Detailed player positions are sourced from Transfermarkt's squad ("kader")
-- pages instead of per-player zerozero pages (which trip zerozero's anti-bot
-- poisoning). roster_sync matches a zerozero team to its Transfermarkt club by
-- name once, then persists the club's TM id here so later runs skip the match.
-- The detailed position itself still flows into players.position / position_code
-- (added by migration_competition_squads.sql) — no new player columns needed.

alter table public.teams
    add column if not exists tm_verein_id text,   -- Transfermarkt club id, e.g. '294'
    add column if not exists tm_slug      text;    -- TM kader slug, e.g. 'sl-benfica'

-- The Transfermarkt competition is configured per competition from the
-- backoffice (no longer hardcoded). tm_competition_code is the TM "Wettbewerb"
-- code (e.g. 'PO1' for Liga Portugal); tm_saison_id optionally overrides the
-- season id, which otherwise derives from competitions.season ('2025/26'->2025).
alter table public.competitions
    add column if not exists tm_competition_code text,
    add column if not exists tm_saison_id        text;

-- Let allow-listed admins set these from the backoffice (mirrors matches_update;
-- service_role still bypasses RLS for the crawler's own writes).
drop policy if exists competitions_update on public.competitions;
create policy competitions_update on public.competitions
    for update to authenticated
    using (public.is_allowed())
    with check (public.is_allowed());
