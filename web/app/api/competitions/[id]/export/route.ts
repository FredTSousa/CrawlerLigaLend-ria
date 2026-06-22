import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { createServiceClient } from "@/lib/supabase/admin";

// Snapshot export (Option C): one self-contained JSON package for a competition
// — metadata + teams + players + fixtures + per-player match stats. Use it to
// seed/reconcile a subscriber (one pull seeds the entire history), or for
// offline diffs.
//
// Auth: either a signed-in admin (cookie, RLS-gated) OR a server-to-server
// bearer token matching EXPORT_API_TOKEN (service-role, RLS bypassed). The
// token path lets a subscriber site (e.g. Liga Lendária) pull without a user
// session.
// GET /api/competitions/{id}/export
export async function GET(
  request: Request,
  { params }: { params: { id: string } },
) {
  const token = process.env.EXPORT_API_TOKEN;
  const auth = request.headers.get("authorization");
  const tokenOk = !!token && auth === `Bearer ${token}`;

  const supabase = tokenOk ? createServiceClient() : createClient();
  if (!tokenOk) {
    const {
      data: { user },
    } = await supabase.auth.getUser();
    if (!user) {
      return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
    }
  }

  const id = params.id;

  const { data: comp } = await supabase
    .from("competitions")
    .select(
      "id, name, full_name, slug, season, fase, epoca_id, source_url, last_sync_at, teams_count, players_count",
    )
    .eq("id", id)
    .maybeSingle();

  if (!comp) {
    return NextResponse.json({ error: "Competition not found" }, { status: 404 });
  }

  const [{ data: ct }, { data: fixtures }] = await Promise.all([
    supabase
      .from("competition_teams")
      .select("team_id, active, source_url, team:teams(id,name,slug,logo_url,source_url)")
      .eq("competition_id", id)
      .eq("active", true),
    supabase
      .from("matches")
      .select(
        "id, round, played_on, status, minute, home_team_id, away_team_id, home_score, away_score, kickoff_at, url, scraped_at, updated_at",
      )
      .eq("competition_id", id)
      .order("round", { ascending: true }),
  ]);

  // Players: page through PostgREST's ~1000-row cap. A 48-team World Cup has
  // ~1250 active players; an unpaginated read returned only the first 1000 and
  // silently dropped ~250 — whole teams. Those teams then had no squad in the
  // snapshot, so the importing site (Liga Lendária) reported every fixture they
  // appeared in as "unlinked club". Order explicitly so range() is deterministic.
  const PAGE = 1000;
  const players: any[] = [];
  for (let from = 0; ; from += PAGE) {
    const { data: page } = await supabase
      .from("competition_player_details")
      .select(
        "player_id, player_name, age, position_group, position, position_code, club_name, team_id, team_name, shirt_number, photo_url, market_value_k, source_url, last_updated",
      )
      .eq("competition_id", id)
      .eq("active", true)
      .order("team_id", { ascending: true })
      .order("player_id", { ascending: true })
      .range(from, from + PAGE - 1);
    if (!page || page.length === 0) break;
    players.push(...page);
    if (page.length < PAGE) break;
  }

  const teams = ((ct ?? []) as any[]).map((r) => ({
    id: r.team?.id ?? r.team_id,
    name: r.team?.name,
    slug: r.team?.slug,
    logo_url: r.team?.logo_url,
    source_url: r.source_url ?? r.team?.source_url,
  }));

  // Per-player match stats (schema_version 2). match_player_details has no
  // competition_id, so filter by this competition's match ids. Page in chunks
  // to stay under PostgREST's ~1000-row cap (a 34-round league is ~8k rows).
  const matchIds = ((fixtures ?? []) as any[]).map((f) => f.id);
  const stats: any[] = [];
  const reporterLinks: any[] = [];
  const matchEvents: any[] = [];
  for (let i = 0; i < matchIds.length; i += 25) {
    const chunk25 = matchIds.slice(i, i + 25);
    const [{ data: statChunk }, { data: reporterChunk }, { data: eventChunk }] = await Promise.all([
      supabase
        .from("match_player_details")
        .select(
          "match_id, round, player_id, player_name, team_id, team_name, order_index, shirt_number, is_captain, is_starter, entered_min, left_min, goals, assists, yellow_cards, red_card, own_goals, penalties_scored, penalties_missed, penalties_defended, played_under_20m, did_not_play, reporter_score, reporter_is_mvp, reporter_linked, reporter_manual",
        )
        .in("match_id", chunk25)
        .order("match_id", { ascending: true })
        .order("order_index", { ascending: true }),
      supabase
        .from("matches_reporter_link")
        .select("match_id, fetched_at, format_detected, urls, home_ratings, away_ratings")
        .in("match_id", chunk25),
      // Per-event minute timeline (the subscriber annotates each goal/card with
      // when it happened). Stored normalized in match_events; we fold it back to
      // a per-player `events` array on each stat row below.
      supabase
        .from("match_events")
        .select("match_id, player_id, event_type, minute, extra_time")
        .in("match_id", chunk25),
    ]);
    if (statChunk) stats.push(...statChunk);
    if (reporterChunk) reporterLinks.push(...reporterChunk);
    if (eventChunk) matchEvents.push(...eventChunk);
  }

  const reporterByMatchId = Object.fromEntries(
    reporterLinks.map((r) => [r.match_id, r]),
  );

  // Fold match_events into a per-player `events: [{type,min,label}]` array keyed
  // by match+player, in the vocabulary the subscriber's importer expects.
  // Subs (sub_in/sub_out) are excluded — those minutes ride on entered_min/left_min.
  const EVENT_TYPE_EXPORT: Record<string, string> = {
    goal: "goal",
    own_goal: "own_goal",
    penalty_scored: "penalty_goal",
    assist: "assist",
    yellow_card: "yellow",
    red_card: "red",
    penalty_missed: "penalty_missed",
    penalty_defended: "penalty_defended",
  };
  const eventsByPlayer = new Map<string, { type: string; min: number; label: string }[]>();
  for (const e of matchEvents as any[]) {
    const type = EVENT_TYPE_EXPORT[e.event_type];
    if (!type) continue; // skip sub_in/sub_out and anything unmapped
    const min = e.minute as number;
    const label = e.extra_time != null ? `${min}+${e.extra_time}` : `${min}`;
    const key = `${e.match_id}:${e.player_id}`;
    const list = eventsByPlayer.get(key) ?? [];
    list.push({ type, min, label });
    eventsByPlayer.set(key, list);
  }
  for (const list of eventsByPlayer.values()) {
    list.sort((a, b) => a.min - b.min);
  }

  const snapshot = {
    schema_version: 2,
    generated_at: new Date().toISOString(),
    competition: {
      id: comp.id,
      zerozero_id: comp.id,
      full_name: comp.full_name,
      name: comp.name,
      season: comp.season,
      slug: comp.slug,
      fase: comp.fase,
      epoca_id: comp.epoca_id,
      source_url: comp.source_url,
      last_sync_at: comp.last_sync_at,
      counts: {
        teams: comp.teams_count ?? teams.length,
        players: comp.players_count ?? players.length,
        fixtures: (fixtures ?? []).length,
        stats: stats.length,
      },
    },
    teams,
    players: players.map((p) => ({
      id: p.player_id,
      name: p.player_name,
      age: p.age,
      position_group: p.position_group,
      position: p.position,
      position_code: p.position_code,
      team_id: p.team_id,
      team_name: p.team_name,
      club_name: p.club_name,
      shirt_number: p.shirt_number,
      photo_url: p.photo_url,
      market_value_k: p.market_value_k,
      source_url: p.source_url,
      last_updated: p.last_updated,
    })),
    fixtures: ((fixtures ?? []) as any[]).map((f) => ({
      ...f,
      reporter: reporterByMatchId[f.id] ?? null,
    })),
    stats: stats.map((s) => ({
      match_id: s.match_id,
      round: s.round,
      player_id: s.player_id,
      player_name: s.player_name,
      team_id: s.team_id,
      team_name: s.team_name,
      order_index: s.order_index,
      shirt_number: s.shirt_number,
      is_captain: s.is_captain,
      is_starter: s.is_starter,
      entered_min: s.entered_min,
      left_min: s.left_min,
      events: eventsByPlayer.get(`${s.match_id}:${s.player_id}`) ?? [],
      goals: s.goals,
      assists: s.assists,
      yellow_cards: s.yellow_cards,
      red_card: s.red_card,
      own_goals: s.own_goals,
      penalties_scored: s.penalties_scored,
      penalties_missed: s.penalties_missed,
      penalties_defended: s.penalties_defended,
      played_under_20m: s.played_under_20m,
      did_not_play: s.did_not_play,
      reporter_score: s.reporter_score,
      reporter_is_mvp: s.reporter_is_mvp,
      reporter_linked: s.reporter_linked,
      reporter_manual: s.reporter_manual,
    })),
  };

  return new NextResponse(JSON.stringify(snapshot, null, 2), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Content-Disposition": `attachment; filename="competition-${id}-snapshot.json"`,
    },
  });
}
