-- Migration: manual reporter links also record the provider raw rating.
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).
--
-- Problem: the goal-ratings page lists match_player_details rows where
-- reporter_raw_score is not null. But reporter_link() (manual linking from the
-- match page) only set reporter_score / reporter_is_mvp / reporter_linked, never
-- reporter_raw_score -- so a hand-linked player (the ones the auto-matcher
-- couldn't resolve) never appeared on the Goal ratings page even though the
-- reporter entry they were linked to carries a raw Goal rating.
--
-- Fix:
--   1) reporter_link() gains p_raw_score and stores it (coalesce: a plain
--      score-edit that passes no raw keeps the existing value).
--   2) Backfill: every already-linked match_player missing a raw_score gets it
--      from its reporter entry in matches_reporter_link, joined on
--      players.abolaid = the rating's player_id (the Goal/A Bola provider id,
--      which manual linking already records on players.abolaid).

-- ----------------------------------------------------------------------------
-- 1) reporter_link() + p_raw_score  (drop old signature first; adding an arg
--    would otherwise create an ambiguous overload for PostgREST)
-- ----------------------------------------------------------------------------
drop function if exists public.reporter_link(text, text, int, boolean, text, text, text);

create or replace function public.reporter_link(
    p_match_id   text,
    p_player_id  text,
    p_score      int,
    p_is_mvp     boolean,
    p_abola_norm text default null,
    p_team_id    text default null,
    p_abola_id   text default null,
    p_raw_score  numeric default null
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
       set reporter_score     = p_score,
           reporter_raw_score = coalesce(p_raw_score, reporter_raw_score),
           reporter_is_mvp    = coalesce(p_is_mvp, false),
           reporter_linked    = true,
           reporter_manual    = true,
           updated_at         = now()
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

grant execute on function
    public.reporter_link(text, text, int, boolean, text, text, text, numeric)
    to authenticated;

-- ----------------------------------------------------------------------------
-- 2) Backfill raw scores onto already-linked players that are missing them.
-- ----------------------------------------------------------------------------
update public.match_players mp
   set reporter_raw_score = sub.raw_score,
       updated_at         = now()
  from (
        select rl.match_id,
               (r->>'player_id')          as ext_id,
               (r->>'raw_score')::numeric as raw_score
          from public.matches_reporter_link rl
          cross join lateral jsonb_array_elements(
              coalesce(rl.home_ratings, '[]'::jsonb)
              || coalesce(rl.away_ratings, '[]'::jsonb)
          ) as r
         where (r->>'raw_score') is not null
           and (r->>'player_id') is not null
       ) sub
  join public.players p on p.abolaid = sub.ext_id
 where mp.match_id        = sub.match_id
   and mp.player_id       = p.id
   and mp.reporter_linked = true
   and mp.reporter_raw_score is null;
