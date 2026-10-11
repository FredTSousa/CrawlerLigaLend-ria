-- Migration: official Liga Portugal stats (ligaportugal.pt) alongside zerozero,
-- for the zerozero-vs-official report page (/official-diff). Run once in the
-- Supabase SQL editor. Safe to re-run (idempotent).
--
-- The league scores goals / assists / yellows / sub minutes from the official
-- site, which zerozero sometimes gets wrong (missed yellows, different assist
-- calls, subs a minute off across the 70' line). ligaportugal.py (job kind
-- 'official') reads the site's public JSON API and stores what it says here.
-- Nothing in this migration changes match_players: the official data is only
-- compared for now, never applied.
--
--   * lp_* columns   -- cached id mapping (ligaportugal.pt <-> zerozero). Players
--                       are mapped by shirt number within a match + team, then
--                       remembered here so later rounds resolve directly.
--   * official_match_players -- per player per match, aggregated from the
--                       official event feed (only players with an event).
--   * official_match_assists / official_assist_totals -- assists aren't in the
--                       event feed: season totals come from the assist
--                       leaderboard, per-match splits from each assister's
--                       match history (fetched only when a total changes).
--   * official_stat_diff -- view comparing both sides per (match, player).

alter table public.competitions
    add column if not exists lp_competition text,   -- e.g. 'ligaportugalbetclic'
    add column if not exists lp_season      text;   -- e.g. '20262027'

alter table public.teams
    add column if not exists lp_team_id text;       -- ligaportugal.pt team external id

alter table public.players
    add column if not exists lp_player_id text;     -- ligaportugal.pt player id

create index if not exists players_lp_player_idx on public.players (lp_player_id);

alter table public.matches
    add column if not exists lp_round           int,
    add column if not exists lp_fixture         int,
    add column if not exists official_synced_at timestamptz,
    add column if not exists official_final     boolean;  -- site's hasOfficialResult

-- ----------------------------------------------------------------------------
-- Per-player official stats for a match
-- ----------------------------------------------------------------------------
create table if not exists public.official_match_players (
    match_id         text not null references public.matches(id) on delete cascade,
    lp_player_id     text not null,
    player_id        text references public.players(id),   -- null = not mapped
    team_id          text references public.teams(id),
    lp_name          text,
    shirt_number     int,
    goals            int     not null default 0,  -- includes penalties
    own_goals        int     not null default 0,
    yellow_cards     int     not null default 0,  -- a second yellow counts (= zerozero's 2)
    red_card         boolean not null default false,
    entered_min      int,
    left_min         int,
    played_under_20m boolean not null default false,
    updated_at       timestamptz not null default now(),
    primary key (match_id, lp_player_id)
);

create index if not exists official_match_players_player_idx
    on public.official_match_players (player_id);

create table if not exists public.official_match_assists (
    match_id     text not null references public.matches(id) on delete cascade,
    lp_player_id text not null,
    assists      int  not null,
    updated_at   timestamptz not null default now(),
    primary key (match_id, lp_player_id)
);

create table if not exists public.official_assist_totals (
    competition_id text not null references public.competitions(id) on delete cascade,
    lp_player_id   text not null,
    lp_name        text,
    lp_team_name   text,
    total          int  not null,          -- leaderboard season total
    detail_total   int,                    -- sum of the per-match split we hold
    fetched_at     timestamptz not null default now(),
    primary key (competition_id, lp_player_id)
);

-- ----------------------------------------------------------------------------
-- RLS: read for approved users; writes only via service_role (ligaportugal.py)
-- ----------------------------------------------------------------------------
alter table public.official_match_players enable row level security;
alter table public.official_match_assists enable row level security;
alter table public.official_assist_totals enable row level security;

drop policy if exists official_match_players_read on public.official_match_players;
create policy official_match_players_read on public.official_match_players
    for select to authenticated using (public.is_allowed());

drop policy if exists official_match_assists_read on public.official_match_assists;
create policy official_match_assists_read on public.official_match_assists
    for select to authenticated using (public.is_allowed());

drop policy if exists official_assist_totals_read on public.official_assist_totals;
create policy official_assist_totals_read on public.official_assist_totals
    for select to authenticated using (public.is_allowed());

-- ----------------------------------------------------------------------------
-- Comparison view: one row per (match, player) on either side, official-synced
-- matches only. zz_* = what we store (manual overrides included), off_* = the
-- official site. off_assists is null when the official per-match split isn't
-- known yet (detail still being fetched, or the player has no ligaportugal id).
-- ----------------------------------------------------------------------------
drop view if exists public.official_stat_diff;
create view public.official_stat_diff
with (security_invoker = true) as
with synced as (
    select id, competition_id, round from public.matches
    where official_synced_at is not null
),
z as (
    select mp.*, s.competition_id, s.round
    from public.match_players mp
    join synced s on s.id = mp.match_id
),
o as (
    select omp.*, s.competition_id, s.round
    from public.official_match_players omp
    join synced s on s.id = omp.match_id
    where omp.player_id is not null
),
j as (
    select
        coalesce(o.match_id, z.match_id)             as match_id,
        coalesce(o.player_id, z.player_id)           as player_id,
        coalesce(o.team_id, z.team_id)               as team_id,
        coalesce(o.competition_id, z.competition_id) as competition_id,
        coalesce(o.round, z.round)                   as round,
        (z.match_id is null)                         as zz_missing,
        coalesce(z.goals, 0) + coalesce(z.penalties_scored, 0) as zz_goals,
        coalesce(o.goals, 0)                         as off_goals,
        coalesce(z.own_goals, 0)                     as zz_own_goals,
        coalesce(o.own_goals, 0)                     as off_own_goals,
        coalesce(z.yellow_cards, 0)                  as zz_yellow_cards,
        coalesce(o.yellow_cards, 0)                  as off_yellow_cards,
        coalesce(z.red_card, false)                  as zz_red_card,
        coalesce(o.red_card, false)                  as off_red_card,
        coalesce(z.played_under_20m, false)          as zz_under_20m,
        coalesce(o.played_under_20m, false)          as off_under_20m,
        z.entered_min as zz_entered_min, z.left_min as zz_left_min,
        o.entered_min as off_entered_min, o.left_min as off_left_min,
        coalesce(z.assists, 0)                       as zz_assists,
        coalesce(z.stat_overrides, '{}'::jsonb)      as stat_overrides
    from o
    full join z on z.match_id = o.match_id and z.player_id = o.player_id
),
x as (
    select
        j.*,
        case
            -- Split held and complete for this player: absent row = 0 in that match.
            when t.total is not null and t.detail_total = t.total
                then coalesce(a.assists, 0)
            -- Known to the site but not on the leaderboard: 0 assists all season.
            when t.total is null and p.lp_player_id is not null then 0
            else null
        end as off_assists
    from j
    left join public.players p on p.id = j.player_id
    left join public.official_assist_totals t
           on t.competition_id = j.competition_id and t.lp_player_id = p.lp_player_id
    left join public.official_match_assists a
           on a.match_id = j.match_id and a.lp_player_id = p.lp_player_id
)
select
    x.*,
    (zz_goals <> off_goals or zz_own_goals <> off_own_goals
     or zz_yellow_cards <> off_yellow_cards or zz_red_card <> off_red_card
     or zz_under_20m <> off_under_20m
     or (off_assists is not null and zz_assists <> off_assists)) as differs
from x;

-- Liga Portugal Betclic 2026/27 (zerozero edition 218294) is scored officially.
update public.competitions
   set lp_competition = 'ligaportugalbetclic', lp_season = '20262027'
 where id = '218294' and lp_competition is null;
