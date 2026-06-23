-- Migration: include the per-event minute timeline in the LIVE webhook payload.
--
-- build_match_event() (the snapshot a subscriber receives on every match.update)
-- built players straight from match_players and never carried the per-event
-- minutes, so the live ingest wrote 0 event_minutes even though match_events was
-- populated. Fold match_events into a per-player `events: [{type,min,label}]`
-- array, in the subscriber's vocabulary, exactly like the snapshot export does.
-- Run once in the Supabase SQL editor. Safe to re-run (create or replace).

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
