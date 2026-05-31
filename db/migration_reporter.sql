-- Migration: A Bola reporter ratings (reporter_score / MVP) for matches.
-- Run once in the Supabase SQL editor. Safe to re-run.

-- A Bola's player id (differs from zerozero's), set when we match a name.
alter table public.players
    add column if not exists abolaid text;

-- Reporter rating attached to a zerozero match_player (best-effort name match).
alter table public.match_players
    add column if not exists reporter_score   int,
    add column if not exists reporter_is_mvp  boolean not null default false;

-- Distinguish reporter (A Bola) crawl runs from zerozero ones on the runs page.
alter table public.crawl_runs
    add column if not exists source text not null default 'zerozero';

-- Raw fetched reporter data + the A Bola URLs used, one row per match.
create table if not exists public.matches_reporter_link (
    match_id        text primary key references public.matches(id) on delete cascade,
    format_detected int,                       -- 1 (crónica) or 2 (notas)
    urls            jsonb,                      -- the A Bola URLs used
    home_ratings    jsonb,                      -- [{player_name,player_id,score,is_mvp,description}]
    away_ratings    jsonb,
    fetched_at      timestamptz not null default now()
);

alter table public.matches_reporter_link enable row level security;
drop policy if exists matches_reporter_link_read on public.matches_reporter_link;
create policy matches_reporter_link_read on public.matches_reporter_link
    for select to authenticated using (public.is_allowed());

-- Expose the reporter fields on the UI view.
drop view if exists public.match_player_details;
create view public.match_player_details
with (security_invoker = on) as
select
    mp.match_id,
    m.round,
    m.played_on,
    mp.player_id,
    p.name              as player_name,
    mp.team_id,
    t.name              as team_name,
    mp.order_index,
    mp.shirt_number,
    mp.is_captain,
    mp.is_starter,
    mp.entered_min,
    mp.left_min,
    mp.goals,
    mp.assists,
    mp.yellow_cards,
    mp.red_card,
    mp.own_goals,
    mp.penalties_scored,
    mp.penalties_missed,
    mp.penalties_defended,
    mp.played_under_20m,
    mp.reporter_score,
    mp.reporter_is_mvp
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;
