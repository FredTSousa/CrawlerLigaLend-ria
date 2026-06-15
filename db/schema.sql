-- ============================================================================
-- CrawlerLigaLendária — Supabase schema
-- ============================================================================
-- Paste this whole file into the Supabase SQL editor and run it once.
-- Safe to re-run: every object uses "if not exists" / "create or replace".
--
-- Model: zerozero.pt IDs are used as natural primary keys (stored as text),
-- so re-crawling UPSERTS instead of duplicating.
--
-- Access: the site is private. All reads are gated by RLS to the e-mail(s)
-- listed in `allowed_users`. The crawler (GitHub Actions) writes using the
-- Supabase service_role key, which bypasses RLS.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- Reference entities
-- ----------------------------------------------------------------------------

create table if not exists public.competitions (
    id          text primary key,              -- zerozero id_edicao, e.g. '201241'
    name        text,                           -- 'Liga Portugal Betclic 2025/26'
    slug        text,                           -- 'liga-portuguesa'
    fase        text,                           -- phase id used in round URLs
    updated_at  timestamptz not null default now()
);

create table if not exists public.teams (
    id          text primary key,              -- zerozero team id, e.g. '1'
    name        text not null,
    updated_at  timestamptz not null default now()
);

create table if not exists public.players (
    id          text primary key,              -- zerozero player id
    name        text not null,
    updated_at  timestamptz not null default now()
);

-- ----------------------------------------------------------------------------
-- Matches
-- ----------------------------------------------------------------------------

create table if not exists public.matches (
    id             text primary key,           -- zerozero game id, e.g. '11071716'
    competition_id text references public.competitions(id),
    round          int,                         -- jornada number
    played_on      date,
    url            text,
    home_team_id   text references public.teams(id),
    away_team_id   text references public.teams(id),
    home_score     int,
    away_score     int,
    status         text not null default 'scheduled', -- scheduled|live|final|postponed
    minute         text,                        -- live clock, e.g. "67'", "HT"
    kickoff_at     timestamptz,                 -- scheduled start time
    watch          boolean not null default false, -- live watcher should follow this
    scraped_at     timestamptz,                 -- when the crawler read this match
    updated_at     timestamptz not null default now()
);

create index if not exists matches_round_idx       on public.matches (round);
create index if not exists matches_competition_idx on public.matches (competition_id, round);
create index if not exists matches_home_team_idx   on public.matches (home_team_id);
create index if not exists matches_away_team_idx   on public.matches (away_team_id);

-- ----------------------------------------------------------------------------
-- Per-player stats for a match (only players who actually played)
-- ----------------------------------------------------------------------------

create table if not exists public.match_players (
    match_id            text not null references public.matches(id) on delete cascade,
    player_id           text not null references public.players(id),
    team_id             text references public.teams(id),
    order_index         int,                    -- lineup order (as on zerozero)
    shirt_number        int,
    is_captain          boolean not null default false,
    is_starter          boolean not null default false,
    entered_min         int,                    -- minute came on (null if started)
    left_min            int,                    -- minute subbed off (null if not)
    goals               int  not null default 0,
    assists             int  not null default 0,
    yellow_cards        int  not null default 0,
    red_card            boolean not null default false,
    own_goals           int  not null default 0,
    penalties_scored    int  not null default 0,
    penalties_missed    int  not null default 0,
    penalties_defended  int  not null default 0,
    played_under_20m    boolean not null default false,
    did_not_play        boolean not null default false,
    updated_at          timestamptz not null default now(),
    primary key (match_id, player_id)
);

create index if not exists match_players_player_idx on public.match_players (player_id);
create index if not exists match_players_team_idx   on public.match_players (team_id);

-- ----------------------------------------------------------------------------
-- Crawl run log (powers the monitoring "run history" panel)
-- ----------------------------------------------------------------------------

create table if not exists public.crawl_runs (
    id             bigint generated always as identity primary key,
    trigger        text,                        -- 'manual' | 'schedule'
    kind           text,                        -- 'round' | 'match'
    target         text,                        -- round number or match url/id
    status         text not null default 'queued', -- queued|running|success|error
    games_count    int,
    error          text,
    github_run_id  text,                         -- link back to the Actions run
    created_at     timestamptz not null default now(),
    started_at     timestamptz,
    finished_at    timestamptz,
    last_seen_at   timestamptz                   -- watcher heartbeat (live_watch.py)
);

create index if not exists crawl_runs_created_idx on public.crawl_runs (created_at desc);

-- ----------------------------------------------------------------------------
-- Access control: allow-list + helper, then RLS policies
-- ----------------------------------------------------------------------------

create table if not exists public.allowed_users (
    email text primary key
);

-- Seed the single allowed login (add more rows later as needed).
insert into public.allowed_users (email)
values ('sousafred@gmail.com')
on conflict (email) do nothing;

-- SECURITY DEFINER so it can read allowed_users without tripping RLS recursion.
create or replace function public.is_allowed()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
    select exists (
        select 1 from public.allowed_users
        where email = (auth.jwt() ->> 'email')
    );
$$;

grant execute on function public.is_allowed() to anon, authenticated;

-- Enable RLS on every table.
alter table public.competitions  enable row level security;
alter table public.teams         enable row level security;
alter table public.players       enable row level security;
alter table public.matches       enable row level security;
alter table public.match_players enable row level security;
alter table public.crawl_runs    enable row level security;
alter table public.allowed_users enable row level security;

-- Read policies: only allow-listed, authenticated users may SELECT.
-- (service_role bypasses RLS, so the crawler can still write.)
do $$
declare t text;
begin
  foreach t in array array[
    'competitions','teams','players','matches','match_players','crawl_runs','allowed_users'
  ] loop
    execute format('drop policy if exists %I on public.%I', t || '_read', t);
    execute format(
      'create policy %I on public.%I for select to authenticated using (public.is_allowed())',
      t || '_read', t);
  end loop;
end $$;

-- Let the site create a "queued" crawl_runs row the instant a button is
-- pressed (the Actions workflow then updates it via service_role).
drop policy if exists crawl_runs_insert on public.crawl_runs;
create policy crawl_runs_insert on public.crawl_runs
    for insert to authenticated
    with check (public.is_allowed());

-- Let the site flag matches for live watching (create/update).
drop policy if exists matches_insert on public.matches;
create policy matches_insert on public.matches
    for insert to authenticated
    with check (public.is_allowed());
drop policy if exists matches_update on public.matches;
create policy matches_update on public.matches
    for update to authenticated
    using (public.is_allowed())
    with check (public.is_allowed());

-- ----------------------------------------------------------------------------
-- Convenience view for the UI (names joined in). security_invoker => RLS applies.
-- ----------------------------------------------------------------------------

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
    mp.did_not_play,
    mp.reporter_score,
    mp.reporter_raw_score,
    mp.reporter_is_mvp,
    mp.reporter_linked,
    mp.reporter_manual
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;
