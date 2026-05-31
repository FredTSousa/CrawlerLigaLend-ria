-- Migration: persistent A Bola -> zerozero player linking + verification.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- Builds on db/migration_reporter.sql (reporter_score / reporter_is_mvp /
-- matches_reporter_link / players.abolaid). Adds:
--   * match_players.reporter_linked / reporter_manual
--   * reporter_aliases  (nickname -> player, so a fix sticks across games)
--   * reporter_link() / reporter_unlink() RPCs (browser-callable, RLS-guarded)
--   * reporter_link_status view (coverage per match)
--   * match_player_details rebuilt with the new columns

-- ----------------------------------------------------------------------------
-- 1) Link state on each match_player
-- ----------------------------------------------------------------------------
-- reporter_linked: a reporter entry was matched to this player. True even when
--   reporter_score is null (an unrated "(-)" player still counts as covered).
-- reporter_manual: a human set/confirmed this row -> re-sync must not clobber it.
alter table public.match_players
    add column if not exists reporter_linked boolean not null default false,
    add column if not exists reporter_manual boolean not null default false;

-- ----------------------------------------------------------------------------
-- 2) Persistent name aliases (A Bola often uses nicknames: "Olinho" = Pohlmann)
-- ----------------------------------------------------------------------------
-- abola_norm is the normalized A Bola name (see public.norm note below). Scoped
-- by team so the same nickname can map to different players across clubs;
-- team_id null = applies to any team.
create table if not exists public.reporter_aliases (
    id          bigint generated always as identity primary key,
    team_id     text references public.teams(id),
    abola_norm  text not null,
    player_id   text not null references public.players(id),
    created_at  timestamptz not null default now(),
    unique (team_id, abola_norm)
);

create index if not exists reporter_aliases_lookup_idx
    on public.reporter_aliases (abola_norm);

alter table public.reporter_aliases enable row level security;
drop policy if exists reporter_aliases_read on public.reporter_aliases;
create policy reporter_aliases_read on public.reporter_aliases
    for select to authenticated using (public.is_allowed());

-- ----------------------------------------------------------------------------
-- 3) Browser-callable mutations (SECURITY DEFINER, gated by is_allowed()).
--    The site has no service-role key, so manual linking goes through these.
-- ----------------------------------------------------------------------------

-- Link (or manually score) a player. p_abola_norm null => pure manual score
-- with no alias (the joined-name / parsing-deviation case). When given, the
-- alias is upserted so the next sync auto-links it; an abola id, when known,
-- is recorded on players.abolaid.
create or replace function public.reporter_link(
    p_match_id   text,
    p_player_id  text,
    p_score      int,
    p_is_mvp     boolean,
    p_abola_norm text default null,
    p_team_id    text default null,
    p_abola_id   text default null
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.is_allowed() then
        raise exception 'not allowed';
    end if;

    update public.match_players
       set reporter_score   = p_score,
           reporter_is_mvp  = coalesce(p_is_mvp, false),
           reporter_linked  = true,
           reporter_manual  = true,
           updated_at       = now()
     where match_id = p_match_id and player_id = p_player_id;

    if p_abola_norm is not null and length(p_abola_norm) > 0 then
        insert into public.reporter_aliases (team_id, abola_norm, player_id)
        values (p_team_id, p_abola_norm, p_player_id)
        on conflict (team_id, abola_norm)
            do update set player_id = excluded.player_id;
    end if;

    if p_abola_id is not null and length(p_abola_id) > 0 then
        update public.players set abolaid = p_abola_id where id = p_player_id;
    end if;
end;
$$;

-- Clear a player's reporter link. Marks reporter_manual so a re-sync won't
-- immediately re-apply a wrong auto-match (the user is taking control).
create or replace function public.reporter_unlink(
    p_match_id  text,
    p_player_id text
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.is_allowed() then
        raise exception 'not allowed';
    end if;

    update public.match_players
       set reporter_score   = null,
           reporter_is_mvp  = false,
           reporter_linked  = false,
           reporter_manual  = true,
           updated_at       = now()
     where match_id = p_match_id and player_id = p_player_id;
end;
$$;

grant execute on function
    public.reporter_link(text, text, int, boolean, text, text, text),
    public.reporter_unlink(text, text)
    to authenticated;

-- ----------------------------------------------------------------------------
-- 4) Coverage per match (powers the round chip and the worklist page).
--    fetched = a reporter scrape has run for this match.
-- ----------------------------------------------------------------------------
drop view if exists public.reporter_link_status;
create view public.reporter_link_status
with (security_invoker = on) as
select
    m.id                                                  as match_id,
    m.round,
    count(mp.*)                                           as players,
    count(mp.*) filter (where mp.reporter_linked)         as linked,
    (rl.match_id is not null)                             as fetched
from public.matches m
join public.match_players mp on mp.match_id = m.id
left join public.matches_reporter_link rl on rl.match_id = m.id
group by m.id, m.round, rl.match_id;

-- ----------------------------------------------------------------------------
-- 5) Expose the new fields on the UI view (keep all existing columns).
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
    mp.reporter_score,
    mp.reporter_is_mvp,
    mp.reporter_linked,
    mp.reporter_manual
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;

-- NOTE: reporter_aliases.abola_norm and the matcher in reporter_sync.py both use
-- abola._norm() (NFKD, strip accents, lowercase, drop non [a-z0-9]). The web app
-- computes the same in web/lib/norm.ts when creating an alias. Keep all three in
-- sync or alias lookups silently miss.
