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

  let body: { kind?: string; target?: string | number; force?: boolean };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Bad request" }, { status: 400 });
  }

  const allowed = ["round", "match", "watch", "reporter", "backfill"];
  const kind = allowed.includes(body.kind as string) ? (body.kind as string) : "round";
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

  // 2) Dispatch the workflow with the run id + crawl target.
  const repo = process.env.GH_REPO;
  const workflow =
    kind === "watch"
      ? process.env.GH_WATCH_WORKFLOW || "watch.yml"
      : kind === "reporter"
        ? process.env.GH_REPORTER_WORKFLOW || "reporter.yml"
        : process.env.GH_WORKFLOW || "crawl.yml";
  const ref = process.env.GH_REF || "main";
  const token = process.env.GH_DISPATCH_TOKEN;

  if (!repo || !token) {
    await supabase
      .from("crawl_runs")
      .update({ status: "error", error: "Server missing GH_REPO / GH_DISPATCH_TOKEN" })
      .eq("id", run.id);
    return NextResponse.json(
      { error: "Server not configured for dispatch" },
      { status: 500 },
    );
  }

  const inputs: Record<string, string> = { run_id: String(run.id) };
  if (kind === "reporter") inputs.match_id = target;
  else if (kind === "match" || kind === "watch") inputs.match = target;
  else if (kind === "backfill") {
    // target is the competition slug or landing-page URL; crawl every
    // not-yet-complete round of that league.
    inputs.competition = target;
    inputs.backfill = "true";
    if (body.force) inputs.force = "true";
  } else inputs.jornada = target;

  const ghRes = await fetch(
    `https://api.github.com/repos/${repo}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref, inputs }),
    },
  );

  if (!ghRes.ok) {
    const text = await ghRes.text();
    await supabase
      .from("crawl_runs")
      .update({ status: "error", error: `dispatch ${ghRes.status}: ${text}`.slice(0, 1000) })
      .eq("id", run.id);
    return NextResponse.json(
      { error: `GitHub dispatch failed (${ghRes.status})` },
      { status: 502 },
    );
  }

  return NextResponse.json({ run_id: run.id });
}
