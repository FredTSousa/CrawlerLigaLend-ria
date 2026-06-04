-- ============================================================================
-- Accelerated live emulation ("replay mode")
-- ============================================================================
-- Replays a finished round as if it were happening live, on an accelerated
-- clock, so a subscriber (e.g. Liga Lendária) can rehearse a real match weekend
-- in ~20 minutes. It works by REWINDING the round's matches/match_players to
-- kickoff and then progressively writing live state on a pg_cron tick — every
-- write rides the existing enqueue triggers -> delivery_outbox -> dispatch, so
-- subscribers receive genuine live `match.update` events. No edge function.
--
-- Clock: matches are grouped by played_on (day). All matches of a day play in
-- parallel over `match_seconds` (default 300s = 5 min); days run sequentially.
-- A round spread over ~4 days therefore takes ~20 min. Scores/stats climb
-- progressively (fake minutes); reporter ratings arrive `reporter_delay_seconds`
-- after full time. The run ends exactly at the round's TRUE final state.
--
-- Requires pg_cron (already enabled for the subscriptions dispatcher). Safe to
-- re-run.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1) State: one run per emulation, plus captured "targets" (the true finals to
--    interpolate toward — captured before the rewind destroys them).
-- ----------------------------------------------------------------------------
create table if not exists public.emulation_runs (
    id                     bigint generated always as identity primary key,
    competition_id         text,
    round                  int,
    status                 text not null default 'running', -- running | done
    match_seconds          int  not null default 300,        -- per-match live window
    reporter_delay_seconds int  not null default 45,         -- A Bola lag after FT
    started_at             timestamptz not null default now(),
    finished_at            timestamptz
);

create index if not exists emulation_runs_active_idx
    on public.emulation_runs (competition_id) where status = 'running';

create table if not exists public.emulation_match_targets (
    run_id      bigint not null references public.emulation_runs(id) on delete cascade,
    match_id    text   not null,
    day_index   int    not null,            -- 0-based calendar-day group within the round
    home_score  int,
    away_score  int,
    primary key (run_id, match_id)
);

create table if not exists public.emulation_player_targets (
    run_id              bigint not null references public.emulation_runs(id) on delete cascade,
    match_id            text   not null,
    player_id           text   not null,
    day_index           int    not null,
    goals               int, assists int, yellow_cards int, red_card boolean,
    own_goals           int, penalties_scored int, penalties_missed int, penalties_defended int,
    played_under_20m    boolean, reporter_score int, reporter_is_mvp boolean,
    primary key (run_id, match_id, player_id)
);

alter table public.emulation_runs            enable row level security;
alter table public.emulation_match_targets   enable row level security;
alter table public.emulation_player_targets  enable row level security;
-- No policies: only the SECURITY DEFINER functions (and service_role) touch these.

-- ----------------------------------------------------------------------------
-- 2) Guard helper: allow allow-listed users OR the service role.
-- ----------------------------------------------------------------------------
create or replace function public.emulation_can_run()
returns boolean language sql stable as $$
    select current_user = 'service_role' or public.is_allowed();
$$;

-- ----------------------------------------------------------------------------
-- 3) Finalize: snap a run's matches/players back to their true finals + done.
--    Used to complete a run and to clean up before starting a fresh one.
-- ----------------------------------------------------------------------------
create or replace function public._emulation_finalize(p_run_id bigint)
returns void language plpgsql security definer set search_path = public as $$
begin
    update public.matches m
       set status = 'final', minute = null,
           home_score = t.home_score, away_score = t.away_score, updated_at = now()
      from public.emulation_match_targets t
     where t.run_id = p_run_id and m.id = t.match_id;

    update public.match_players mp
       set goals = pt.goals, assists = pt.assists, yellow_cards = pt.yellow_cards,
           red_card = pt.red_card, own_goals = pt.own_goals,
           penalties_scored = pt.penalties_scored, penalties_missed = pt.penalties_missed,
           penalties_defended = pt.penalties_defended, played_under_20m = pt.played_under_20m,
           reporter_score = pt.reporter_score, reporter_is_mvp = pt.reporter_is_mvp,
           reporter_linked = (pt.reporter_score is not null), updated_at = now()
      from public.emulation_player_targets pt
     where pt.run_id = p_run_id and mp.match_id = pt.match_id and mp.player_id = pt.player_id;

    update public.emulation_runs set status = 'done', finished_at = now()
     where id = p_run_id and status = 'running';
end $$;

-- ----------------------------------------------------------------------------
-- 4) start_emulation(): capture finals, rewind to kickoff, begin the clock.
-- ----------------------------------------------------------------------------
create or replace function public.start_emulation(
    p_competition_id text,
    p_round          int,
    p_match_seconds  int default 300,
    p_reporter_delay int default 45
) returns bigint language plpgsql security definer set search_path = public as $$
declare
    v_run_id bigint;
    v_old    bigint;
begin
    if not public.emulation_can_run() then raise exception 'not allowed'; end if;

    -- Restore + close any in-flight run for this competition (avoids capturing a
    -- mid-live state as the new "finals").
    for v_old in select id from public.emulation_runs
                 where competition_id = p_competition_id and status = 'running' loop
        perform public._emulation_finalize(v_old);
    end loop;

    insert into public.emulation_runs (competition_id, round, match_seconds, reporter_delay_seconds)
    values (p_competition_id, p_round, p_match_seconds, p_reporter_delay)
    returning id into v_run_id;

    -- Capture match targets + assign a 0-based day group per distinct played_on.
    insert into public.emulation_match_targets (run_id, match_id, day_index, home_score, away_score)
    select v_run_id, m.id,
           dense_rank() over (order by m.played_on nulls last) - 1,
           m.home_score, m.away_score
      from public.matches m
     where m.competition_id = p_competition_id and m.round = p_round;

    if not exists (select 1 from public.emulation_match_targets where run_id = v_run_id) then
        update public.emulation_runs set status = 'done', finished_at = now() where id = v_run_id;
        raise exception 'no matches found for competition % round %', p_competition_id, p_round;
    end if;

    -- Capture player targets (inherit the match's day_index).
    insert into public.emulation_player_targets (
        run_id, match_id, player_id, day_index,
        goals, assists, yellow_cards, red_card, own_goals,
        penalties_scored, penalties_missed, penalties_defended,
        played_under_20m, reporter_score, reporter_is_mvp)
    select v_run_id, mp.match_id, mp.player_id, t.day_index,
           mp.goals, mp.assists, mp.yellow_cards, mp.red_card, mp.own_goals,
           mp.penalties_scored, mp.penalties_missed, mp.penalties_defended,
           mp.played_under_20m, mp.reporter_score, mp.reporter_is_mvp
      from public.match_players mp
      join public.emulation_match_targets t
        on t.run_id = v_run_id and t.match_id = mp.match_id;

    -- Rewind to kickoff (lineup rows stay; event stats + scores zeroed).
    update public.matches
       set status = 'scheduled', minute = null, home_score = null, away_score = null, updated_at = now()
     where competition_id = p_competition_id and round = p_round;

    update public.match_players mp
       set goals = 0, assists = 0, yellow_cards = 0, red_card = false, own_goals = 0,
           penalties_scored = 0, penalties_missed = 0, penalties_defended = 0,
           played_under_20m = false, reporter_score = null, reporter_is_mvp = false,
           reporter_linked = false, updated_at = now()
      from public.emulation_match_targets t
     where t.run_id = v_run_id and mp.match_id = t.match_id;

    return v_run_id;
end $$;

-- ----------------------------------------------------------------------------
-- 5) stop_emulation(): finalize every running run for a competition now.
-- ----------------------------------------------------------------------------
create or replace function public.stop_emulation(p_competition_id text)
returns void language plpgsql security definer set search_path = public as $$
declare v_id bigint;
begin
    if not public.emulation_can_run() then raise exception 'not allowed'; end if;
    for v_id in select id from public.emulation_runs
                where competition_id = p_competition_id and status = 'running' loop
        perform public._emulation_finalize(v_id);
    end loop;
end $$;

-- ----------------------------------------------------------------------------
-- 6) emulation_tick(): advance every running run. Driven by pg_cron (~10s).
--    Phases per match, by elapsed vs its day window [d*ms, (d+1)*ms):
--      before -> scheduled (already rewound)   in window -> live (progressive)
--      after  -> final (true)                  after+delay -> reporter revealed
-- ----------------------------------------------------------------------------
create or replace function public.emulation_tick()
returns void language plpgsql security definer set search_path = public as $$
declare
    r         record;
    v_elapsed numeric;
    v_days    int;
    ms        int;
begin
    for r in select * from public.emulation_runs where status = 'running' loop
        ms := r.match_seconds;
        v_elapsed := extract(epoch from (now() - r.started_at));
        select coalesce(max(day_index) + 1, 0) into v_days
          from public.emulation_match_targets where run_id = r.id;

        -- LIVE matches: ticking minute + climbing score.
        update public.matches m
           set status = 'live',
               minute = floor(least(1, (v_elapsed - t.day_index*ms)/ms) * 94)::text || '''',
               home_score = least(t.home_score, floor(((v_elapsed - t.day_index*ms)/ms) * (t.home_score + 1)))::int,
               away_score = least(t.away_score, floor(((v_elapsed - t.day_index*ms)/ms) * (t.away_score + 1)))::int,
               updated_at = now()
          from public.emulation_match_targets t
         where t.run_id = r.id and m.id = t.match_id
           and v_elapsed >= t.day_index*ms and v_elapsed < (t.day_index + 1)*ms;

        -- LIVE players: climbing counts (k = floor(f * (final+1)), capped).
        update public.match_players mp
           set goals = least(pt.goals, floor(f.frac * (pt.goals + 1)))::int,
               assists = least(pt.assists, floor(f.frac * (pt.assists + 1)))::int,
               yellow_cards = least(pt.yellow_cards, floor(f.frac * (pt.yellow_cards + 1)))::int,
               own_goals = least(pt.own_goals, floor(f.frac * (pt.own_goals + 1)))::int,
               penalties_scored = least(pt.penalties_scored, floor(f.frac * (pt.penalties_scored + 1)))::int,
               penalties_missed = least(pt.penalties_missed, floor(f.frac * (pt.penalties_missed + 1)))::int,
               penalties_defended = least(pt.penalties_defended, floor(f.frac * (pt.penalties_defended + 1)))::int,
               updated_at = now()
          from public.emulation_player_targets pt
          cross join lateral (select (v_elapsed - pt.day_index*ms)/ms as frac) f
         where pt.run_id = r.id and mp.match_id = pt.match_id and mp.player_id = pt.player_id
           and v_elapsed >= pt.day_index*ms and v_elapsed < (pt.day_index + 1)*ms;

        -- FINAL matches (guarded so it fires once).
        update public.matches m
           set status = 'final', minute = null,
               home_score = t.home_score, away_score = t.away_score, updated_at = now()
          from public.emulation_match_targets t
         where t.run_id = r.id and m.id = t.match_id
           and v_elapsed >= (t.day_index + 1)*ms
           and (m.status is distinct from 'final'
                or m.home_score is distinct from t.home_score
                or m.away_score is distinct from t.away_score);

        -- FINAL players (full stats minus reporter; guarded so it fires once).
        update public.match_players mp
           set goals = pt.goals, assists = pt.assists, yellow_cards = pt.yellow_cards,
               red_card = pt.red_card, own_goals = pt.own_goals,
               penalties_scored = pt.penalties_scored, penalties_missed = pt.penalties_missed,
               penalties_defended = pt.penalties_defended, played_under_20m = pt.played_under_20m,
               updated_at = now()
          from public.emulation_player_targets pt
         where pt.run_id = r.id and mp.match_id = pt.match_id and mp.player_id = pt.player_id
           and v_elapsed >= (pt.day_index + 1)*ms
           and (mp.goals is distinct from pt.goals or mp.assists is distinct from pt.assists
                or mp.yellow_cards is distinct from pt.yellow_cards or mp.red_card is distinct from pt.red_card
                or mp.own_goals is distinct from pt.own_goals
                or mp.penalties_scored is distinct from pt.penalties_scored
                or mp.penalties_missed is distinct from pt.penalties_missed
                or mp.penalties_defended is distinct from pt.penalties_defended
                or mp.played_under_20m is distinct from pt.played_under_20m);

        -- REPORTER ratings (after FT + delay; guarded so it fires once).
        update public.match_players mp
           set reporter_score = pt.reporter_score, reporter_is_mvp = pt.reporter_is_mvp,
               reporter_linked = (pt.reporter_score is not null), updated_at = now()
          from public.emulation_player_targets pt
         where pt.run_id = r.id and mp.match_id = pt.match_id and mp.player_id = pt.player_id
           and v_elapsed >= (pt.day_index + 1)*ms + r.reporter_delay_seconds
           and mp.reporter_linked = false and pt.reporter_score is not null;

        -- Complete once the last day's window + reporter delay has fully elapsed.
        if v_elapsed >= v_days*ms + r.reporter_delay_seconds + 5 then
            perform public._emulation_finalize(r.id);
        end if;
    end loop;
end $$;

grant execute on function public.start_emulation(text, int, int, int) to authenticated, service_role;
grant execute on function public.stop_emulation(text)                 to authenticated, service_role;
grant execute on function public.emulation_tick()                     to authenticated, service_role;

-- ----------------------------------------------------------------------------
-- 7) Schedule the tick (~10s). Requires pg_cron. Idempotent.
-- ----------------------------------------------------------------------------
do $$ begin
    perform cron.unschedule('emulation_tick');
exception when others then null; end $$;

select cron.schedule('emulation_tick', '10 seconds', $$select public.emulation_tick();$$);
