-- Migration: first round the zerozero-vs-official report cares about. Run once
-- in the Supabase SQL editor. Safe to re-run (idempotent).
--
-- The league only counts from a given round, so ligaportugal.py skips earlier
-- rounds and /official-diff hides them. Null = every round.

alter table public.competitions
    add column if not exists official_from_round int;

update public.competitions
   set official_from_round = 8
 where id = '218294' and official_from_round is null;
