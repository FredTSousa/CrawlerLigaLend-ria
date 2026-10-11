-- Migration: record what zerozero changes after the whistle. Run once in the
-- Supabase SQL editor, AFTER db/migration_official_history.sql. Safe to re-run.
--
-- The hourly settle sweep (sync.py run_settle_sweep) re-crawls each finished
-- match ~14 h after kickoff. It now snapshots the match's zerozero values just
-- before and after that re-crawl and stores every difference here -- the
-- zerozero counterpart of official_stat_history, so /official-diff shows how
-- much each source still moves after the end. Values are zerozero's own (a
-- stat overridden by hand is read from stat_crawled), so a ✎ fix never shows
-- up as a zerozero change.

create table if not exists public.zerozero_stat_history (
    id          bigint generated always as identity primary key,
    match_id    text not null references public.matches(id) on delete cascade,
    player_id   text not null references public.players(id),
    stat        text not null,  -- goals|penalties_scored|own_goals|assists|yellow_cards|red_card|entered_min|left_min
    old_value   int,            -- null = player wasn't in the lineup before
    new_value   int,            -- null = player dropped from the lineup
    observed_at timestamptz not null default now()
);

create index if not exists zerozero_stat_history_match_idx
    on public.zerozero_stat_history (match_id);

alter table public.zerozero_stat_history enable row level security;

drop policy if exists zerozero_stat_history_read on public.zerozero_stat_history;
create policy zerozero_stat_history_read on public.zerozero_stat_history
    for select to authenticated using (public.is_allowed());
