// ingest-example — REFERENCE implementation for a SUBSCRIBER site.
//
// Copy this into the *subscriber's* Supabase project (not the crawler's). It
// receives a signed match-update from the crawler's `dispatch` function,
// verifies the HMAC, and upserts the snapshot into the subscriber's own tables
// (see db/subscriber_schema.example.sql). Idempotent: re-deliveries are safe.
//
// Set the shared secret on the subscriber side:
//   supabase secrets set CRAWLER_WEBHOOK_SECRET=<same value as subscribers.secret>
//
// Deploy:  supabase functions deploy ingest-example --no-verify-jwt
// Then register its URL in the crawler DB:
//   insert into subscribers (label, competition_id, callback_url, secret)
//   values ('my-site', '201241',
//           'https://<their-ref>.functions.supabase.co/ingest-example',
//           '<same secret>');

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SECRET = Deno.env.get("CRAWLER_WEBHOOK_SECRET")!;
const db = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);

const encoder = new TextEncoder();

async function verify(body: string, header: string | null): Promise<boolean> {
  if (!header?.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(SECRET),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const provided = header.slice("sha256=".length);
  // hex -> bytes
  const bytes = new Uint8Array(
    provided.match(/.{1,2}/g)?.map((h) => parseInt(h, 16)) ?? [],
  );
  return await crypto.subtle.verify(
    "HMAC",
    key,
    bytes,
    encoder.encode(body),
  );
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method", { status: 405 });

  // Read the RAW body and verify BEFORE parsing (sign-then-parse).
  const raw = await req.text();
  if (!(await verify(raw, req.headers.get("X-Signature")))) {
    return new Response("bad signature", { status: 401 });
  }

  const evt = JSON.parse(raw);
  const m = evt.match;

  // Upsert the match (mirror only the columns this site needs).
  const { error: mErr } = await db.from("liga_matches").upsert({
    id: m.id,
    competition_id: evt.competition?.id ?? null,
    round: m.round,
    played_on: m.played_on,
    status: m.status,
    minute: m.minute,
    kickoff_at: m.kickoff_at,
    home_team_id: m.home_team?.id ?? null,
    home_team_name: m.home_team?.name ?? null,
    away_team_id: m.away_team?.id ?? null,
    away_team_name: m.away_team?.name ?? null,
    home_score: m.home_score,
    away_score: m.away_score,
    updated_at: new Date().toISOString(),
  });
  if (mErr) return new Response(`match: ${mErr.message}`, { status: 500 });

  // Replace this match's player rows with the snapshot (full-state, so any
  // dropped player disappears too).
  await db.from("liga_match_players").delete().eq("match_id", m.id);
  const players = (evt.players ?? []).map((p: Record<string, unknown>) => ({
    match_id: m.id,
    ...p,
  }));
  if (players.length) {
    const { error: pErr } = await db.from("liga_match_players").insert(players);
    if (pErr) return new Response(`players: ${pErr.message}`, { status: 500 });
  }

  return Response.json({ ok: true, match_id: m.id, players: players.length });
});
