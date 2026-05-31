-- EXAMPLE schema for a SUBSCRIBER site's own Supabase project.
-- Run this in the *subscriber's* project (not the crawler's). The
-- ingest-example Edge Function upserts into these tables. Adapt freely —
-- the subscriber owns this schema and only mirrors the columns it cares about.
--
-- The crawler's zerozero ids are used as natural keys, so re-deliveries upsert
-- instead of duplicating.

create table if not exists public.liga_matches (
    id              text primary key,            -- zerozero game id
    competition_id  text,
    round           int,
    played_on       date,
    status          text,                         -- scheduled|live|final|postponed
    minute          text,                         -- live clock, e.g. "67'", "HT"
    kickoff_at      timestamptz,
    home_team_id    text,
    home_team_name  text,
    away_team_id    text,
    away_team_name  text,
    home_score      int,
    away_score      int,
    updated_at      timestamptz not null default now()
);

create index if not exists liga_matches_comp_idx
    on public.liga_matches (competition_id, round);

create table if not exists public.liga_match_players (
    match_id            text not null references public.liga_matches(id) on delete cascade,
    player_id           text not null,
    player_name         text,
    team_id             text,
    order_index         int,
    shirt_number        int,
    is_captain          boolean,
    is_starter          boolean,
    entered_min         int,
    left_min            int,
    goals               int,
    assists             int,
    yellow_cards        int,
    red_card            boolean,
    own_goals           int,
    penalties_scored    int,
    penalties_missed    int,
    penalties_defended  int,
    played_under_20m    boolean,
    reporter_score      int,
    reporter_is_mvp     boolean,
    primary key (match_id, player_id)
);

create index if not exists liga_match_players_player_idx
    on public.liga_match_players (player_id);
