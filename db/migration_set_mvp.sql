-- Migration: edit the reporter MVP by hand from the match details page.
-- Run once in the Supabase SQL editor. Safe to re-run (idempotent).
--
-- The reporter scrape marks the "melhor em campo" as match_players.reporter_is_mvp,
-- but there was no way to FIX a missed or wrong MVP -- e.g. when A Bola flags the
-- standout in a layout the scraper couldn't read. This adds a browser-callable
-- RPC (SECURITY DEFINER, gated by is_allowed(), like reporter_link) that sets one
-- player as the match's MVP, clearing any prior MVP anywhere in the match (MVP is
-- match-wide: a single best-on-the-pitch across both teams). Pass p_player_id =
-- null to just clear the match's MVP.
--
-- Both the cleared old MVP and the new one are marked reporter_manual=true so the
-- 30-min reporter sweep / re-sync (which skips reporter_manual rows and re-applies
-- the scraped MVP to the rest) keeps the human's choice instead of clobbering it.

-- Drop the earlier per-team signature, if a draft of it was ever created, so
-- PostgREST doesn't see an ambiguous overload.
drop function if exists public.reporter_set_mvp(text, text, text);

create or replace function public.reporter_set_mvp(
    p_match_id  text,
    p_player_id text default null
) returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if not public.is_allowed() then
        raise exception 'not allowed';
    end if;

    -- Drop any current MVP in the match; freeze those rows so a re-sync can't
    -- re-mark the scraped MVP over the human's edit.
    update public.match_players
       set reporter_is_mvp = false,
           reporter_manual = true,
           updated_at      = now()
     where match_id = p_match_id
       and reporter_is_mvp;

    -- Crown the chosen player (when one was given).
    if p_player_id is not null and length(p_player_id) > 0 then
        update public.match_players
           set reporter_is_mvp = true,
               reporter_manual = true,
               updated_at      = now()
         where match_id  = p_match_id
           and player_id = p_player_id;
    end if;
end;
$$;

grant execute on function
    public.reporter_set_mvp(text, text)
    to authenticated;
