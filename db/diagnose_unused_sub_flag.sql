-- Diagnose the "unused substitute recorded as played" bug (Martin Turk /
-- Estoril Praia 1-1 FC Famalicão, Liga Portugal Betclic 2026/27 round 1).
--
-- Root cause (fixed in crawler.py, parse_player_block()): the unused-sub
-- flag was read with `chunk.startswith(" inactive")`, a fixed-position
-- prefix check against the player card's `class` attribute. zerozero emits
-- an extra position class ahead of "inactive" on some cards -- goalkeepers
-- in particular (their bench card carries a distinct GK styling class) --
-- e.g. `class="player goalkeeper inactive ..."` instead of
-- `class="player inactive ..."`. The prefix check missed those, so an
-- unused backup keeper (or anyone else whose card happens to order classes
-- that way) got did_not_play = false with no entered_min/left_min: exactly
-- what the downstream ingest reads as "played the full match". The fix
-- reads the full class list instead of a fixed position.
--
-- This file is READ-ONLY diagnostics. It is deliberately not a scripted
-- UPDATE: the correct repair is a full re-crawl of each affected match with
-- the fixed parser (see the runbook at the bottom), not hand-flipping
-- did_not_play in place -- that would leave order_index/entered_min/etc.
-- stale and wouldn't catch a same-match mis-parse of a *different* field.

-- 1) Exact match: Estoril Praia vs FC Famalicão, round 1.
select
    mpd.match_id,
    mpd.round,
    mpd.played_on,
    mpd.team_name,
    mpd.player_name,
    mpd.is_starter,
    mpd.entered_min,
    mpd.left_min,
    mpd.did_not_play,
    m.url as zerozero_url
from public.match_player_details mpd
join public.matches m on m.id = mpd.match_id
where mpd.player_name ilike '%turk%'
   or (mpd.round = 1
       and (mpd.team_name ilike '%estoril%' or mpd.team_name ilike '%famalic%'));

-- 2) Blast radius: every row across every match/competition with the same
-- signature -- benched (not a starter), no substitution timing recorded,
-- yet not flagged as an unused sub. A real player who came on always gets
-- entered_min from the "Entrou" event, and a starter is is_starter = true,
-- so this combination should only exist because of the parsing bug above.
select
    mpd.match_id,
    mpd.round,
    mpd.played_on,
    mpd.team_name,
    mpd.player_name,
    m.url as zerozero_url
from public.match_player_details mpd
join public.matches m on m.id = mpd.match_id
where mpd.is_starter = false
  and mpd.entered_min is null
  and mpd.left_min is null
  and mpd.did_not_play = false
order by mpd.played_on desc nulls last, mpd.match_id, mpd.player_name;

-- ----------------------------------------------------------------------------
-- Repair runbook
-- ----------------------------------------------------------------------------
-- For every match_id returned by query (2) above (Estoril-Famalicão included),
-- re-crawl that single match with the fixed crawler.py/sync.py so ALL of its
-- player rows -- not just the flagged one -- get regenerated correctly:
--
--   python sync.py --match "<zerozero_url from the query above>"
--
-- That UPSERTs match_players for the whole lineup, which unconditionally
-- fires trg_match_players_enqueue -> delivery_outbox -> the `dispatch` edge
-- function -> a signed `match.update` POST to every active subscriber for
-- that match's competition (db/migration_subscriptions.sql). No manual
-- resync should be needed on the subscriber side once this re-crawl lands --
-- the existing webhook does it.
