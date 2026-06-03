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
