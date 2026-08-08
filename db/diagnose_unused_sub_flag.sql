-- Diagnostics for the "unused substitute shows as played" report (Martin Turk
-- / Estoril Praia 1-1 FC Famalicão, Liga Portugal Betclic 2026/27 round 1).
--
-- UPDATE: the initial theory here (a goalkeeper-specific class-ordering bug
-- in crawler.py's parse_player_block) did not hold up against the real
-- zerozero markup -- Turk's card is `class="player inactive fl-r-cen"`,
-- which the parser (before AND after that hardening) reads correctly, and
-- his match_players row has always had did_not_play = true. Storage was
-- never wrong for this match.
--
-- The actual root cause was in the OUTBOUND webhook payload, not scraping:
-- public.build_match_event() (the function `dispatch` calls to build the
-- match.update body sent to subscribers) never included `did_not_play` in
-- its per-player object -- not since it was first defined, and not after the
-- did_not_play column/view were added later. A subscriber deriving
-- `played = !did_not_play` from a payload that's missing the key entirely
-- reads every unused sub, in every match, in every league, as "played".
-- Fixed in db/migration_match_event_did_not_play.sql -- apply that, then use
-- select public.replay_competition(...) there to force a fresh, corrected
-- delivery of already-played matches (nothing here needs re-scraping).
--
-- What's left below is still useful as a genuine STORAGE sanity check (in
-- case a *future* scrape ever does mis-set did_not_play), just not what
-- caused this particular report.

-- 1) Turk / this match, current stored values.
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

-- 2) Storage sanity check: benched, no substitution timing, yet not flagged
-- as an unused sub. A real player who came on always gets entered_min from
-- the "Entrou" event, and a starter is is_starter = true, so this
-- combination shouldn't otherwise exist. Empty result = storage is fine
-- (expected, given the finding above) and the fix is purely the migration.
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
