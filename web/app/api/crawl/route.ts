import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

// Triggers a crawl: verifies the user, records a queued crawl_runs row, then
// dispatches the GitHub Actions workflow (which the self-hosted runner picks
// up). The dispatch token is server-only and never reaches the browser.
export async function POST(request: Request) {
  const supabase = createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user) {
    return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  }

  let body: {
    kind?: string;
    target?: string | number;
    force?: boolean;
    full?: boolean;
    competition?: string;
    team?: string;
    abolaUrl?: string;
  };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Bad request" }, { status: 400 });
  }

  // Squad kinds drive roster_sync.py via squads.yml; the rest drive sync.py.
  const squadKinds = ["teams", "roster", "players", "comp_full", "comp_players"];
  const allowed = [
    "round", "match", "watch", "reporter", "backfill", ...squadKinds,
  ];
  const kind = allowed.includes(body.kind as string) ? (body.kind as string) : "round";
  const isSquad = squadKinds.includes(kind);
  const target = String(body.target ?? "").trim();
  if (!target) {
    return NextResponse.json({ error: "Missing target" }, { status: 400 });
  }
  const source = kind === "reporter" ? "abola" : "zerozero";

  // 1) Record a queued run (RLS allows the approved user to insert).
  const { data: run, error: insErr } = await supabase
    .from("crawl_runs")
    .insert({ trigger: "manual", kind, target, status: "queued", source })
    .select("id")
    .single();

  if (insErr || !run) {
    return NextResponse.json(
      { error: insErr?.message || "Could not create run" },
      { status: 500 },
    );
  }

  // For a watch, flag the match in the DB so the daemon picks it up. Create a
  // stub row if it doesn't exist yet (teams fill in on the first crawl).
  if (kind === "watch") {
    const idMatch = target.match(/\/(\d+)(?:[/?#]|$)/);
    const matchId = idMatch ? idMatch[1] : null;
    if (!matchId) {
      return NextResponse.json({ error: "Invalid match URL" }, { status: 400 });
    }
    const { error: upErr } = await supabase
      .from("matches")
      .upsert({ id: matchId, url: target, watch: true }, { onConflict: "id" });
    if (upErr) {
      return NextResponse.json({ error: upErr.message }, { status: 500 });
    }
  }

  // 2) Build the job params (mirroring the old workflow inputs) + a dedupe key.
  //    The dedupe key collapses a duplicate request for the same target while
  //    one is already pending/running (the partial unique index in
  //    db/migration_job_queue.sql), which is the collision safety the old
  //    single-runner FIFO gave us implicitly. params.run_id ties the job back to
  //    the crawl_runs row above so the worker updates it (status polling works).
  //    Lower priority = claimed sooner; live watching jumps the queue.
  const params: Record<string, unknown> = { run_id: run.id };
  let dedupe: string;
  let priority = 100;
  if (kind === "watch") {
    // One singleton daemon follows every match flagged watch=true, so repeated
    // "Watch live" clicks just (re)flag a match and reuse the same job.
    dedupe = "watch:daemon";
    priority = 10;
  } else if (kind === "reporter") {
    params.match_id = target;
    const abolaUrl = String(body.abolaUrl ?? "").trim();
    if (abolaUrl) {
      if (abolaUrl.includes("goal.com")) params.goal_url = abolaUrl;
      else params.abola_url = abolaUrl;
    }
    dedupe = `reporter:${target}`;
    priority = 60;
  } else if (kind === "match") {
    params.match = target;
    dedupe = `match:${target}`;
    priority = 50;
  } else if (kind === "backfill") {
    params.competition = target; // comp slug/URL; crawl every open round
    if (body.force) params.force = true;
    dedupe = `backfill:${target}`;
    priority = 90;
  } else if (isSquad) {
    // Squad syncs (roster_sync.py): teams/comp_full/comp_players/roster/players.
    if (kind === "players") {
      params.player = target;
      dedupe = `players:${target}`;
    } else {
      const comp = body.competition || target;
      params.competition = comp;
      if (kind === "comp_full") params.full = true;
      if (kind === "comp_players") params.players_only = true;
      if (kind === "roster" && body.team) params.team = body.team;
      if (body.force) params.force = true;
      dedupe = `${kind}:${comp}${body.team ? `:${body.team}` : ""}`;
    }
    priority = 70;
  } else {
    // round
    params.jornada = target;
    if (body.competition) params.competition = body.competition;
    dedupe = `round:${body.competition || "default"}:${target}`;
    priority = 50;
  }

  // 3) Enqueue the job (deduped). Workers pull it via claim_job (SKIP LOCKED).
  const { data: jobId, error: enqErr } = await supabase.rpc("enqueue_job", {
    p_kind: kind,
    p_params: params,
    p_dedupe_key: dedupe,
    p_priority: priority,
  });
  if (enqErr) {
    await supabase
      .from("crawl_runs")
      .update({ status: "error", error: `enqueue: ${enqErr.message}`.slice(0, 1000) })
      .eq("id", run.id);
    return NextResponse.json({ error: enqErr.message }, { status: 500 });
  }

  // 4) Wake a worker now for low latency. This is best-effort: the job is already
  //    queued, and the worker workflow's scheduled backstop will pick it up even
  //    if this dispatch fails, so a failure here is non-fatal.
  const repo = process.env.GH_REPO;
  const workflow = process.env.GH_WORKER_WORKFLOW || "worker.yml";
  const ref = process.env.GH_REF || "main";
  const token = process.env.GH_DISPATCH_TOKEN;
  if (repo && token) {
    await fetch(
      `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref }),
      },
    ).catch(() => {/* backstop cron will start a worker */});
  }

  return NextResponse.json({ run_id: run.id, job_id: jobId });
}
