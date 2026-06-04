import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { createServiceClient } from "@/lib/supabase/admin";

// Start (or stop) an accelerated live emulation of a round — see
// db/migration_emulation.sql. Lets a subscriber site (Liga Lendária) rehearse a
// match weekend in ~20 minutes; every replayed write rides the normal
// dispatch webhook, so subscribers receive live `match.update` events.
//
// Auth: signed-in admin (cookie) OR server-to-server bearer EXPORT_API_TOKEN.
//
// POST /api/competitions/{id}/emulate
//   { "round": 31 }                          -> start
//   { "round": 31, "match_seconds": 300, "reporter_delay": 45 }
//   { "stop": true }                         -> finalize any running run now
export async function POST(
  request: Request,
  { params }: { params: { id: string } },
) {
  const token = process.env.EXPORT_API_TOKEN;
  const auth = request.headers.get("authorization");
  const tokenOk = !!token && auth === `Bearer ${token}`;

  const supabase = tokenOk ? createServiceClient() : createClient();
  if (!tokenOk) {
    const { data: { user } } = await supabase.auth.getUser();
    if (!user) return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  }

  const competitionId = params.id;
  const body = (await request.json().catch(() => ({}))) as {
    round?: number;
    match_seconds?: number;
    reporter_delay?: number;
    stop?: boolean;
  };

  if (body.stop) {
    const { error } = await supabase.rpc("stop_emulation", { p_competition_id: competitionId });
    if (error) return NextResponse.json({ error: error.message }, { status: 500 });
    return NextResponse.json({ ok: true, stopped: true });
  }

  if (typeof body.round !== "number") {
    return NextResponse.json({ error: "round (number) is required" }, { status: 400 });
  }

  const { data, error } = await supabase.rpc("start_emulation", {
    p_competition_id: competitionId,
    p_round: body.round,
    p_match_seconds: body.match_seconds ?? 300,
    p_reporter_delay: body.reporter_delay ?? 45,
  });
  if (error) return NextResponse.json({ error: error.message }, { status: 500 });

  return NextResponse.json({ ok: true, run_id: data, round: body.round });
}
