-- Migration: official Liga Portugal stats WIN over zerozero, automatically.
-- Run once in the Supabase SQL editor, AFTER db/migration_stat_overrides.sql and
-- db/migration_official_no_assists.sql. Safe to re-run (idempotent).
--
-- zerozero sometimes only corrects a match up to ~48 h later, while the league
-- scores from ligaportugal.pt. So once ligaportugal.py has the official events
-- of a match, they are what match_players (and therefore the UI, the export and
-- the subscriber webhooks) carries. Priority per stat:
--
--     manual ✎ (stat_overrides)  >  official (stat_official)  >  zerozero
--
--   * match_players.stat_official jsonb -- the official values for the row, in
--     official terms: goals (penalties INCLUDED), own_goals, yellow_cards,
--     red_card, plus entered_min / left_min when the official feed has a sub
--     event for the player. Written only by apply_official_stats() below.
--     Assists are not in it: they stay on zerozero.
--   * match_players.stat_crawled jsonb  -- now zerozero's FULL raw snapshot of
--     the tracked stats, sent by sync.py on every crawl write (it used to hold
--     only the overridden keys). That keeps zerozero's own values available to
--     /official-diff, the settle-sweep history and the ✎ "reset".
--   * The BEFORE trigger (replaced) applies official, then manual, on EVERY
--     write, so a zerozero re-crawl can never put its draft values back. When a
--     stat stops being forced, it falls back to zerozero's value in stat_crawled.
--     Goals: the stored split stays zerozero's -- goals = official total minus
--     zerozero's penalties_scored (capped at the total). A forced sub minute
--     also re-derives played_under_20m (on at 70'+ or off before 20').
--   * apply_official_stats(match) -- maps official_match_players onto the
--     match's rows. A zerozero player with no official row gets zeros (the
--     official feed lists every goal and card), except on a team with an
--     official event we couldn't map to a zerozero player: that team keeps
--     zerozero's values for unlisted players. Rounds before
--     competitions.official_from_round are never touched.
--   * official_stat_diff -- zz_* are now zerozero's raw values (stat_crawled),
--     so the page keeps showing what zerozero got wrong. It also compares raw
--     sub minutes (supersedes db/migration_official_sub_minutes.sql).
--
-- The backfill at the bottom applies every already-fetched match; each changed
-- match is re-sent to subscribers through the usual match_players triggers.

alter table public.match_players
    add column if not exists stat_official jsonb not null default '{}'::jsonb;

-- ----------------------------------------------------------------------------
-- Which columns a row's manual + official values force.
-- ----------------------------------------------------------------------------
create or replace function public.match_players_forced_cols(p_manual jsonb, p_official jsonb)
returns text[]
language sql
immutable
as $$
    select coalesce(array_agg(distinct c), '{}')
    from (
        select m.k as c
          from jsonb_object_keys(coalesce(p_manual, '{}'::jsonb)) as m(k)
         where m.k in ('goals', 'assists', 'yellow_cards')
        union all
        select unnest(case o.k
                   when 'goals'        then array['goals', 'penalties_scored']
                   when 'own_goals'    then array['own_goals']
                   when 'yellow_cards' then array['yellow_cards']
                   when 'red_card'     then array['red_card']
                   when 'entered_min'  then array['entered_min', 'played_under_20m']
                   when 'left_min'     then array['left_min', 'played_under_20m']
                   else array[]::text[] end)
          from jsonb_object_keys(coalesce(p_official, '{}'::jsonb)) as o(k)
    ) s;
$$;

-- ----------------------------------------------------------------------------
-- Trigger: official, then manual, on every write.
-- ----------------------------------------------------------------------------
create or replace function public.match_players_apply_overrides()
returns trigger
language plpgsql
as $$
declare
    man      jsonb := coalesce(new.stat_overrides, '{}'::jsonb);
    off      jsonb := coalesce(new.stat_official, '{}'::jsonb);
    now_cols text[] := public.match_players_forced_cols(new.stat_overrides, new.stat_official);
    was_cols text[] := '{}';
    k        text;
    total    int;
    pens     int;
begin
    if tg_op = 'UPDATE' then
        was_cols := public.match_players_forced_cols(old.stat_overrides, old.stat_official);
    end if;
    if now_cols = '{}' and was_cols = '{}' then
        return new;
    end if;
    new.stat_crawled := coalesce(new.stat_crawled, '{}'::jsonb);

    -- zerozero's value behind a column about to be forced, when the crawl
    -- snapshot doesn't carry it (rows last crawled before this migration): a
    -- column that wasn't forced until now still holds zerozero's value.
    foreach k in array now_cols loop
        if not (new.stat_crawled ? k) and not (k = any(was_cols)) then
            new.stat_crawled := new.stat_crawled || jsonb_build_object(k, to_jsonb(new) -> k);
        end if;
    end loop;

    -- No longer forced: back to zerozero's value.
    foreach k in array was_cols loop
        if not (k = any(now_cols)) and new.stat_crawled ? k then
            new := jsonb_populate_record(new, jsonb_build_object(k, new.stat_crawled -> k));
        end if;
    end loop;

    -- Official.
    if off ? 'goals' then
        total := (off ->> 'goals')::int;
        pens := least(coalesce((new.stat_crawled ->> 'penalties_scored')::int, 0), total);
        new.penalties_scored := pens;
        new.goals := total - pens;
    end if;
    if off ? 'own_goals'    then new.own_goals    := (off ->> 'own_goals')::int; end if;
    if off ? 'yellow_cards' then new.yellow_cards := (off ->> 'yellow_cards')::int; end if;
    if off ? 'red_card'     then new.red_card     := (off ->> 'red_card')::boolean; end if;
    if off ? 'entered_min'  then new.entered_min  := (off ->> 'entered_min')::int; end if;
    if off ? 'left_min'     then new.left_min     := (off ->> 'left_min')::int; end if;
    if off ? 'entered_min' or off ? 'left_min' then
        new.played_under_20m := coalesce(new.entered_min >= 70, false)
                             or coalesce(new.left_min < 20, false);
    end if;

    -- Manual last: a ✎ fix beats everything.
    if man ? 'goals'        then new.goals        := (man ->> 'goals')::int; end if;
    if man ? 'assists'      then new.assists      := (man ->> 'assists')::int; end if;
    if man ? 'yellow_cards' then new.yellow_cards := (man ->> 'yellow_cards')::int; end if;
    return new;
end;
$$;

drop trigger if exists match_players_apply_overrides on public.match_players;
create trigger match_players_apply_overrides
    before insert or update on public.match_players
    for each row execute function public.match_players_apply_overrides();

-- ----------------------------------------------------------------------------
-- RPC: set / clear one manual override. The trigger now does the restoring
-- (to the official value if there is one, else zerozero's), so this only
-- edits stat_overrides.
-- ----------------------------------------------------------------------------
create or replace function public.reporter_set_stat(
    p_match_id  text,
    p_player_id text,
    p_stat      text,
    p_value     int default null
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.is_allowed() then
        raise exception 'not allowed';
    end if;
    if p_stat not in ('goals', 'assists', 'yellow_cards') then
        raise exception 'unsupported stat %', p_stat;
    end if;
    if p_value is not null and (p_value < 0 or p_value > 9) then
        raise exception 'value out of range';
    end if;

    update public.match_players
       set stat_overrides = case when p_value is null then stat_overrides - p_stat
                                 else stat_overrides || jsonb_build_object(p_stat, p_value) end,
           updated_at     = now()
     where match_id = p_match_id and player_id = p_player_id;
    if not found then
        raise exception 'player not in match';
    end if;
end;
$$;

grant execute on function
    public.reporter_set_stat(text, text, text, int)
    to authenticated;

-- ----------------------------------------------------------------------------
-- apply_official_stats(match): official_match_players -> match_players.stat_official.
-- Returns how many rows changed. Called by ligaportugal.py after every fetch.
-- ----------------------------------------------------------------------------
create or replace function public.apply_official_stats(p_match_id text)
returns int
language plpgsql
security definer
set search_path = public
as $$
declare
    counts boolean;
    n      int;
begin
    -- Only fetched matches (with official events) the league counts.
    select m.official_synced_at is not null
           and coalesce(m.round, 0) >= coalesce(c.official_from_round, 1)
           and exists (select 1 from public.official_match_players x where x.match_id = m.id)
      into counts
      from public.matches m
      left join public.competitions c on c.id = m.competition_id
     where m.id = p_match_id;

    with o as (
        select omp.*, count(*) over (partition by omp.player_id) as dup
          from public.official_match_players omp
         where omp.match_id = p_match_id and omp.player_id is not null
    ),
    want as (
        select mp.player_id,
               case
                   when not coalesce(counts, false) then '{}'::jsonb
                   -- Two official players on one zerozero row: ambiguous, leave it.
                   when o.dup > 1 then '{}'::jsonb
                   when o.player_id is not null then jsonb_strip_nulls(jsonb_build_object(
                        'goals', o.goals, 'own_goals', o.own_goals,
                        'yellow_cards', o.yellow_cards, 'red_card', o.red_card,
                        'entered_min', o.entered_min, 'left_min', o.left_min))
                   -- An official event on this team we couldn't place: the
                   -- missing goal/card may be this player's.
                   when exists (select 1 from public.official_match_players u
                                 where u.match_id = p_match_id and u.player_id is null
                                   and u.team_id is not distinct from mp.team_id)
                       then '{}'::jsonb
                   else '{"goals": 0, "own_goals": 0, "yellow_cards": 0, "red_card": false}'::jsonb
               end as v
          from public.match_players mp
          left join (select distinct on (player_id) * from o) o on o.player_id = mp.player_id
         where mp.match_id = p_match_id
    )
    update public.match_players mp
       set stat_official = w.v,
           updated_at    = now()
      from want w
     where mp.match_id = p_match_id
       and mp.player_id = w.player_id
       and mp.stat_official is distinct from w.v;
    get diagnostics n = row_count;
    return n;
end;
$$;

revoke execute on function public.apply_official_stats(text) from public;
grant execute on function public.apply_official_stats(text) to service_role;

-- ----------------------------------------------------------------------------
-- Expose stat_official to the UI.
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
    mp.reporter_manual,
    mp.stat_overrides,
    mp.stat_crawled,
    mp.stat_official
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;

-- ----------------------------------------------------------------------------
-- Comparison view: zerozero's RAW values (stat_crawled where held) vs official.
-- ----------------------------------------------------------------------------
drop view if exists public.official_stat_diff;
create view public.official_stat_diff
with (security_invoker = true) as
with synced as (
    select id, competition_id, round from public.matches
    where official_synced_at is not null
),
z as (
    select mp.match_id, mp.player_id, mp.team_id, s.competition_id, s.round,
           mp.stat_overrides,
           -- zerozero's own value: the crawl snapshot, else the column.
           to_jsonb(mp) || coalesce(mp.stat_crawled, '{}'::jsonb) as raw
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
        coalesce((z.raw ->> 'goals')::int, 0)
          + coalesce((z.raw ->> 'penalties_scored')::int, 0) as zz_goals,
        coalesce(o.goals, 0)                         as off_goals,
        coalesce((z.raw ->> 'own_goals')::int, 0)    as zz_own_goals,
        coalesce(o.own_goals, 0)                     as off_own_goals,
        coalesce((z.raw ->> 'yellow_cards')::int, 0) as zz_yellow_cards,
        coalesce(o.yellow_cards, 0)                  as off_yellow_cards,
        coalesce((z.raw ->> 'red_card')::boolean, false) as zz_red_card,
        coalesce(o.red_card, false)                  as off_red_card,
        coalesce((z.raw ->> 'played_under_20m')::boolean, false) as zz_under_20m,
        coalesce(o.played_under_20m, false)          as off_under_20m,
        (z.raw ->> 'entered_min')::int as zz_entered_min,
        (z.raw ->> 'left_min')::int    as zz_left_min,
        o.entered_min as off_entered_min, o.left_min as off_left_min,
        coalesce(z.stat_overrides, '{}'::jsonb)      as stat_overrides
    from o
    full join z on z.match_id = o.match_id and z.player_id = o.player_id
)
select
    j.*,
    (zz_entered_min is distinct from off_entered_min
     or zz_left_min is distinct from off_left_min) as minutes_differ,
    (zz_goals <> off_goals or zz_own_goals <> off_own_goals
     or zz_yellow_cards <> off_yellow_cards or zz_red_card <> off_red_card
     or zz_under_20m <> off_under_20m
     or zz_entered_min is distinct from off_entered_min
     or zz_left_min is distinct from off_left_min) as differs
from j;

-- ----------------------------------------------------------------------------
-- Backfill: apply every match fetched so far.
-- ----------------------------------------------------------------------------
select count(*) filter (where n > 0) as matches_changed, sum(n) as rows_changed
from (
    select public.apply_official_stats(id) as n
      from public.matches
     where official_synced_at is not null
) s;
