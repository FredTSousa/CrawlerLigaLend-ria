-- Migration: per-competition scheduled-crawl flag.
-- Run once in the Supabase SQL editor. Safe to re-run (additive + idempotent).
--
-- The 6-hourly "Crawl & sync" sweep loops every competition and skips the ones
-- that need no update (current round fully scheduled in the future with known
-- kickoff times, or all rounds final). When a competition's season is complete
-- (all rounds final) the sweep sets crawl_active=false so it stops touching it
-- entirely. A manual backfill of the competition re-activates it.

alter table public.competitions
    add column if not exists crawl_active boolean not null default true;

comment on column public.competitions.crawl_active is
    'Scheduled crawl sweep includes this competition while true; set false when '
    'the season is complete (all rounds final). Manual backfill re-activates it.';
