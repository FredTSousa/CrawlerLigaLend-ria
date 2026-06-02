import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

// Snapshot export (Option C): one self-contained JSON package for a competition
// — metadata + teams + players + fixtures. Signed-in admins only (RLS applies to
// every read below). Use it to seed/reconcile a subscriber, or for offline diffs.
// GET /api/competitions/{id}/export
export async function GET(
  _request: Request,
  { params }: { params: { id: string } },
) {
  const supabase = createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
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

  const [{ data: ct }, { data: players }, { data: fixtures }] =
    await Promise.all([
      supabase
        .from("competition_teams")
        .select("team_id, active, source_url, team:teams(id,name,slug,logo_url,source_url)")
        .eq("competition_id", id)
        .eq("active", true),
      supabase
        .from("competition_player_details")
        .select(
          "player_id, player_name, age, position_group, position, position_code, club_name, team_id, team_name, shirt_number, source_url, last_updated",
        )
        .eq("competition_id", id)
        .eq("active", true),
      supabase
        .from("matches")
        .select(
          "id, round, played_on, status, home_team_id, away_team_id, home_score, away_score, kickoff_at, url",
        )
        .eq("competition_id", id)
        .order("round", { ascending: true }),
    ]);

  const teams = ((ct ?? []) as any[]).map((r) => ({
    id: r.team?.id ?? r.team_id,
    name: r.team?.name,
    slug: r.team?.slug,
    logo_url: r.team?.logo_url,
    source_url: r.source_url ?? r.team?.source_url,
  }));

  const snapshot = {
    schema_version: 1,
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
        players: comp.players_count ?? (players ?? []).length,
        fixtures: (fixtures ?? []).length,
      },
    },
    teams,
    players: ((players ?? []) as any[]).map((p) => ({
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
      source_url: p.source_url,
      last_updated: p.last_updated,
    })),
    fixtures: (fixtures ?? []) as any[],
  };

  return new NextResponse(JSON.stringify(snapshot, null, 2), {
    status: 200,
    headers: {
      "Content-Type": "application/json",
      "Content-Disposition": `attachment; filename="competition-${id}-snapshot.json"`,
    },
  });
}
