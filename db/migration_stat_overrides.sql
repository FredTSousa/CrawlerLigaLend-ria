-- Migration: hand-correct a player's crawled stats (goals / assists / yellows)
-- from the match details page. Run once in the Supabase SQL editor. Safe to
-- re-run (idempotent).
--
-- zerozero occasionally publishes wrong per-player stats (e.g. a yellow card on
-- the wrong player). Like the other manual edits (reporter_link, set_mvp), a
-- human fix must survive every later re-crawl / live-watch write.
--
-- How it works:
--   * match_players.stat_overrides  jsonb  {"yellow_cards": 1, "goals": 0, ...}
--     -- the human's values, keyed by column name.
--   * match_players.stat_crawled    jsonb  the last value the crawler wrote for
--     each overridden column, so clearing an override can restore it without
--     waiting for a re-crawl.
--   * A BEFORE INSERT/UPDATE trigger re-applies stat_overrides on EVERY write,
--     so sync.py / live_watch / emulation need no changes: whatever they upsert,
--     the stored column (and therefore the export, the webhook payload and the
--     UI) carries the override. The crawler's upsert never sends
--     stat_overrides, so ON CONFLICT DO UPDATE keeps the existing overrides.
--   * reporter_set_stat(match, player, stat, value) sets (value >= 0) or clears
--     (value null) one override; gated by is_allowed() like the other RPCs.

alter table public.match_players
    add column if not exists stat_overrides jsonb not null default '{}'::jsonb,
    add column if not exists stat_crawled   jsonb not null default '{}'::jsonb;

-- ----------------------------------------------------------------------------
-- Trigger: apply overrides on every write.
-- ----------------------------------------------------------------------------
create or replace function public.match_players_apply_overrides()
returns trigger
language plpgsql
as $$
declare
    k       text;
    v       int;
    incoming int;
    editing boolean := coalesce(current_setting('app.stat_edit', true), '') = 'on';
begin
    if new.stat_overrides is null or new.stat_overrides = '{}'::jsonb then
        return new;
    end if;

    for k, v in
        select key, value::int from jsonb_each_text(new.stat_overrides)
    loop
        if k not in ('goals', 'assists', 'yellow_cards') then
            continue;
        end if;
        incoming := (to_jsonb(new) ->> k)::int;
        -- A non-RPC write that actually changes the column is the crawler
        -- reporting a (new) value: remember it so a cleared override can
        -- fall back to it.
        if not editing
           and tg_op = 'UPDATE'
           and incoming is distinct from (to_jsonb(old) ->> k)::int then
            new.stat_crawled := new.stat_crawled || jsonb_build_object(k, incoming);
        end if;
        new := jsonb_populate_record(new, jsonb_build_object(k, v));
    end loop;
    return new;
end;
$$;

drop trigger if exists match_players_apply_overrides on public.match_players;
create trigger match_players_apply_overrides
    before insert or update on public.match_players
    for each row execute function public.match_players_apply_overrides();

-- ----------------------------------------------------------------------------
-- RPC: set / clear one override.
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
declare
    r public.match_players%rowtype;
    restore int;
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

    select * into r from public.match_players
     where match_id = p_match_id and player_id = p_player_id
     for update;
    if not found then
        raise exception 'player not in match';
    end if;

    -- Tell the trigger this write is the human edit, not a crawl.
    perform set_config('app.stat_edit', 'on', true);

    if p_value is not null then
        update public.match_players
           set stat_overrides = stat_overrides || jsonb_build_object(p_stat, p_value),
               -- First override on this stat: the current value is the crawled one.
               stat_crawled   = case when stat_crawled ? p_stat then stat_crawled
                                     else stat_crawled || jsonb_build_object(p_stat, (to_jsonb(r) ->> p_stat)::int)
                                end,
               updated_at     = now()
         where match_id = p_match_id and player_id = p_player_id;
    else
        restore := coalesce((r.stat_crawled ->> p_stat)::int, (to_jsonb(r) ->> p_stat)::int);
        -- Drop the override and restore the crawled value in one write (the
        -- override key is gone from NEW, so the trigger leaves the column alone).
        execute format(
            'update public.match_players
                set %I = $1,
                    stat_overrides = stat_overrides - $4,
                    stat_crawled   = stat_crawled - $4,
                    updated_at     = now()
              where match_id = $2 and player_id = $3',
            p_stat)
          using restore, p_match_id, p_player_id, p_stat;
    end if;

    perform set_config('app.stat_edit', 'off', true);
end;
$$;

grant execute on function
    public.reporter_set_stat(text, text, text, int)
    to authenticated;

-- ----------------------------------------------------------------------------
-- Expose the overrides to the UI.
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
    mp.stat_crawled
from public.match_players mp
join public.matches m on m.id = mp.match_id
join public.players p on p.id = mp.player_id
left join public.teams t on t.id = mp.team_id;
