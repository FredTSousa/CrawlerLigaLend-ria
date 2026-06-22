-- Add match_events table: one row per discrete in-match event occurrence
-- (goal, own_goal, penalty_scored, assist, yellow_card, red_card,
--  penalty_missed, penalty_defended, sub_in, sub_out) with its exact minute.
--
-- The crawler replaces all rows for a match on each crawl (DELETE + INSERT)
-- so no UPSERT key is needed here.

create table if not exists public.match_events (
    id          bigint generated always as identity primary key,
    match_id    text not null references public.matches(id) on delete cascade,
    player_id   text not null references public.players(id),
    team_id     text references public.teams(id),
    event_type  text not null,  -- goal|own_goal|penalty_scored|assist|yellow_card|red_card|penalty_missed|penalty_defended|sub_in|sub_out
    minute      int  not null,
    extra_time  int,            -- added minutes (e.g. 2 for "90+2'"), null when absent
    updated_at  timestamptz not null default now()
);

create index if not exists match_events_match_idx  on public.match_events (match_id);
create index if not exists match_events_player_idx on public.match_events (player_id);
create index if not exists match_events_type_idx   on public.match_events (match_id, event_type);

-- RLS
alter table public.match_events enable row level security;

drop policy if exists match_events_read on public.match_events;
create policy match_events_read on public.match_events
    for select to authenticated using (public.is_allowed());
