-- build_match_event() has never sent `did_not_play` in the "players" array of
-- the match.update webhook payload, even though the column has existed on
-- match_players since migration_did_not_play.sql (and was already exposed on
-- the match_player_details VIEW by migration_did_not_play_exclude.sql). The
-- webhook builder is a separate code path from that view and was never
-- updated to include it -- true for its original definition
-- (migration_subscriptions.sql, 2026-06-14) and for its most recent
-- redefinition (migration_match_event_minutes.sql, 2026-06-23), which added
-- per-event minutes but carried the same field list forward.
--
-- Net effect: every unused substitute, in every match, in every league, has
-- always gone out over the webhook with no did_not_play key at all. A
-- subscriber deriving `played = !did_not_play` from the raw payload reads a
-- missing key as `!undefined` -> true, so every benched-but-unused player is
-- ingested as "played". This is the actual root cause behind the Martin Turk
-- report -- his match_players row was correct the whole time; the outbound
-- payload just dropped the flag that says so.
--
-- This migration only adds the missing field (copy of the current function
-- body from migration_match_event_minutes.sql, `create or replace` so it's
-- safe to re-run). It does not change anything else about the payload shape.
create or replace function public.build_match_event(p_match_id text)
returns jsonb
language sql
stable
as $$
    select jsonb_build_object(
        'event', 'match.update',
        'match_id', m.id,
        'sent_at', now(),
        'competition', case when c.id is null then null else jsonb_build_object(
            'id', c.id, 'name', c.name, 'slug', c.slug, 'fase', c.fase) end,
        'match', jsonb_build_object(
            'id', m.id,
            'round', m.round,
            'played_on', m.played_on,
            'url', m.url,
            'status', m.status,
            'minute', m.minute,
            'kickoff_at', m.kickoff_at,
            'home_team', case when ht.id is null then null else
                jsonb_build_object('id', ht.id, 'name', ht.name) end,
            'away_team', case when at.id is null then null else
                jsonb_build_object('id', at.id, 'name', at.name) end,
            'home_score', m.home_score,
            'away_score', m.away_score,
            'scraped_at', m.scraped_at,
            'updated_at', m.updated_at),
        'players', coalesce((
            select jsonb_agg(jsonb_build_object(
                'player_id', mp.player_id,
                'player_name', p.name,
                'team_id', mp.team_id,
                'order_index', mp.order_index,
                'shirt_number', mp.shirt_number,
                'is_captain', mp.is_captain,
                'is_starter', mp.is_starter,
                'entered_min', mp.entered_min,
                'left_min', mp.left_min,
                -- Unused substitute (never entered): true iff the player was
                -- on the bench and never came on. See the summary above --
                -- this is the field the webhook was missing.
                'did_not_play', mp.did_not_play,
                -- Per-event minute timeline, folded from match_events into the
                -- subscriber's vocabulary (penalty_scored->penalty_goal,
                -- yellow_card->yellow, red_card->red); subs excluded (they ride
                -- on entered_min/left_min). Keeps stoppage in the label ("90+2").
                'events', coalesce((
                    select jsonb_agg(jsonb_build_object(
                        'type', case me.event_type
                                  when 'penalty_scored' then 'penalty_goal'
                                  when 'yellow_card'    then 'yellow'
                                  when 'red_card'       then 'red'
                                  else me.event_type end,
                        'min', me.minute,
                        'label', me.minute::text ||
                                 case when me.extra_time is not null
                                      then '+' || me.extra_time::text else '' end)
                        order by me.minute, me.extra_time nulls first)
                    from public.match_events me
                    where me.match_id = mp.match_id
                      and me.player_id = mp.player_id
                      and me.event_type not in ('sub_in','sub_out')), '[]'::jsonb),
                'goals', mp.goals,
                'assists', mp.assists,
                'yellow_cards', mp.yellow_cards,
                'red_card', mp.red_card,
                'own_goals', mp.own_goals,
                'penalties_scored', mp.penalties_scored,
                'penalties_missed', mp.penalties_missed,
                'penalties_defended', mp.penalties_defended,
                'played_under_20m', mp.played_under_20m,
                'reporter_score', mp.reporter_score,
                'reporter_is_mvp', mp.reporter_is_mvp)
                order by mp.order_index nulls last)
            from public.match_players mp
            join public.players p on p.id = mp.player_id
            where mp.match_id = m.id), '[]'::jsonb),
        'reporter', (
            select case when rl.match_id is null then null else jsonb_build_object(
                'fetched_at', rl.fetched_at,
                'format_detected', rl.format_detected,
                'urls', rl.urls,
                'home_ratings', rl.home_ratings,
                'away_ratings', rl.away_ratings) end
            from public.matches_reporter_link rl where rl.match_id = m.id)
    )
    from public.matches m
    left join public.competitions c on c.id = m.competition_id
    left join public.teams ht on ht.id = m.home_team_id
    left join public.teams at on at.id = m.away_team_id
    where m.id = p_match_id;
$$;

grant execute on function public.build_match_event(text) to service_role;

-- ----------------------------------------------------------------------------
-- Backfill: this is a payload-contract fix, not a data fix -- match_players
-- rows are already correct, so nothing will naturally re-trigger the
-- match_players_enqueue trigger for historical matches. Force a fresh
-- snapshot of every match so subscribers receive the corrected payload
-- (now including did_not_play) and can re-derive `played` for every unused
-- sub they'd previously ingested as "played". Scope to one competition or
-- pass NULL for every league; safe to re-run.
--
--   select public.replay_competition(null);          -- every league
--   select public.replay_competition('201241');       -- just Liga Portugal Betclic
