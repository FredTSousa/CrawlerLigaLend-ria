// dispatch — fan out match-change events to league subscribers.
//
// Invoked by a Database Webhook on delivery_outbox INSERT (low latency) and by
// a pg_cron backstop every ~30s (retries). The request body is ignored: each
// invocation drains whatever is currently claimable, so duplicate triggers are
// harmless.
//
// For each claimed outbox row it:
//   1. builds the full snapshot via build_match_event(match_id),
//   2. finds active subscribers for that competition (NULL competition = all),
//   3. POSTs the JSON body to each, signed with HMAC-SHA256 (X-Signature),
//   4. marks the row delivered, or schedules a backed-off retry on failure.
//
// Env (auto-provided to Supabase Edge Functions):
//   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
//
// Deploy:  supabase functions deploy dispatch --no-verify-jwt
// (--no-verify-jwt because pg_net/pg_cron call it with the service-role key in
//  the Authorization header, not an end-user JWT.)

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const MAX_ATTEMPTS = 8;
const BATCH = 50;
const TIMEOUT_MS = 10_000;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const db = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { persistSession: false },
});

type Outbox = {
  id: number;
  match_id: string;
  competition_id: string | null;
  attempts: number;
};

type Subscriber = {
  id: number;
  callback_url: string;
  secret: string;
  competition_id: string | null;
};

const encoder = new TextEncoder();

async function hmacHex(secret: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(body));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

// Exponential backoff with a cap: ~5s, 10s, 20s, ... up to 5 min.
function backoffSeconds(attempts: number): number {
  return Math.min(5 * 2 ** Math.max(0, attempts - 1), 300);
}

async function postSigned(
  sub: Subscriber,
  matchId: string,
  body: string,
): Promise<void> {
  const signature = await hmacHex(sub.secret, body);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const res = await fetch(sub.callback_url, {
      method: "POST",
      signal: ctrl.signal,
      headers: {
        "Content-Type": "application/json",
        "X-Signature": `sha256=${signature}`,
        "X-Event-Type": "match.update",
        "X-Match-Id": matchId,
      },
      body,
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(
        `subscriber ${sub.id} -> ${res.status}: ${text.slice(0, 300)}`,
      );
    }
  } finally {
    clearTimeout(timer);
  }
}

async function deliver(row: Outbox): Promise<void> {
  // 1) Build the current snapshot for this match.
  const { data: payload, error: buildErr } = await db.rpc("build_match_event", {
    p_match_id: row.match_id,
  });
  if (buildErr) throw new Error(`build_match_event: ${buildErr.message}`);
  if (!payload) {
    // Match vanished (deleted). Nothing to send — consider it done.
    await markDelivered(row.id);
    return;
  }

  // 2) Subscribers for this league (or wildcard NULL = every league).
  let q = db.from("subscribers").select("id,callback_url,secret,competition_id")
    .eq("active", true);
  q = row.competition_id
    ? q.or(`competition_id.eq.${row.competition_id},competition_id.is.null`)
    : q.is("competition_id", null);
  const { data: subs, error: subErr } = await q;
  if (subErr) throw new Error(`subscribers: ${subErr.message}`);

  if (!subs || subs.length === 0) {
    await markDelivered(row.id); // no listeners; don't pile up
    return;
  }

  // 3) Sign + POST to each. One signature per subscriber (distinct secrets).
  const body = JSON.stringify(payload);
  const results = await Promise.allSettled(
    (subs as Subscriber[]).map((s) => postSigned(s, row.match_id, body)),
  );
  const failures = results.filter((r) => r.status === "rejected") as
    PromiseRejectedResult[];

  if (failures.length === 0) {
    await markDelivered(row.id);
  } else {
    // Idempotent ingest means re-sending to the ones that already succeeded is
    // safe, so we retry the whole row.
    throw new Error(failures.map((f) => String(f.reason)).join(" | "));
  }
}

async function markDelivered(id: number): Promise<void> {
  await db.from("delivery_outbox").update({
    status: "delivered",
    delivered_at: new Date().toISOString(),
    last_error: null,
    updated_at: new Date().toISOString(),
  }).eq("id", id);
}

async function markRetry(row: Outbox, error: string): Promise<void> {
  const dead = row.attempts >= MAX_ATTEMPTS;
  const next = new Date(Date.now() + backoffSeconds(row.attempts) * 1000);
  await db.from("delivery_outbox").update({
    status: dead ? "failed" : "pending",
    last_error: error.slice(0, 2000),
    next_attempt_at: next.toISOString(),
    updated_at: new Date().toISOString(),
  }).eq("id", row.id);
}

Deno.serve(async () => {
  const { data: claimed, error } = await db.rpc("claim_outbox_batch", {
    p_limit: BATCH,
  });
  if (error) {
    return new Response(`claim error: ${error.message}`, { status: 500 });
  }
  const rows = (claimed ?? []) as Outbox[];

  let delivered = 0, retried = 0;
  for (const row of rows) {
    try {
      await deliver(row);
      delivered++;
    } catch (err) {
      retried++;
      await markRetry(row, String((err as Error)?.message ?? err));
    }
  }

  return Response.json({ claimed: rows.length, delivered, retried });
});
