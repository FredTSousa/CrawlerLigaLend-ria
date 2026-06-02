// dispatch — fan out match-change AND squad-change events to league subscribers.
//
// Invoked by a Database Webhook on delivery_outbox / entity_outbox INSERT (low
// latency) and by a pg_cron backstop every ~30s (retries). The request body is
// ignored: each invocation drains whatever is currently claimable from BOTH
// outboxes, so duplicate triggers are harmless.
//
//   delivery_outbox  -> build_match_event(match_id)          -> match.update
//   entity_outbox    -> build_competition_event(comp)        -> CompetitionUpdated
//                    -> build_team_event(team, comp)         -> RosterUpdated
//                    -> build_player_event(player, comp)     -> PlayerUpdated
//                    -> build_comp_sync_event(comp)          -> CompetitionSyncCompleted
//
// For each claimed row it builds the current snapshot, finds active subscribers
// for that competition (NULL competition = all), POSTs the JSON body signed with
// HMAC-SHA256 (X-Signature), and marks delivered or schedules a backed-off retry.
//
// Env (auto-provided): SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
// Deploy:  supabase functions deploy dispatch --no-verify-jwt

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const MAX_ATTEMPTS = 8;
const BATCH = 50;
const TIMEOUT_MS = 10_000;

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const db = createClient(SUPABASE_URL, SERVICE_KEY, {
  auth: { persistSession: false },
});

type MatchOutbox = {
  id: number;
  match_id: string;
  competition_id: string | null;
  attempts: number;
};

type EntityOutbox = {
  id: number;
  entity_type: "competition" | "team" | "player" | "comp_sync";
  entity_id: string;
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
  eventType: string,
  refId: string,
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
        "X-Event-Type": eventType,
        "X-Entity-Id": refId,
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

// Active subscribers for a competition (or wildcard NULL = every league).
async function subscribersFor(
  competitionId: string | null,
): Promise<Subscriber[]> {
  let q = db.from("subscribers").select("id,callback_url,secret,competition_id")
    .eq("active", true);
  q = competitionId
    ? q.or(`competition_id.eq.${competitionId},competition_id.is.null`)
    : q.is("competition_id", null);
  const { data, error } = await q;
  if (error) throw new Error(`subscribers: ${error.message}`);
  return (data ?? []) as Subscriber[];
}

// Sign + POST a built payload to all matching subscribers. Throws if any failed
// (idempotent ingest makes re-sending to the ones that succeeded harmless).
async function fanout(
  payload: unknown,
  competitionId: string | null,
  eventType: string,
  refId: string,
): Promise<"no-listeners" | "ok"> {
  const subs = await subscribersFor(competitionId);
  if (subs.length === 0) return "no-listeners";
  const body = JSON.stringify(payload);
  const results = await Promise.allSettled(
    subs.map((s) => postSigned(s, eventType, refId, body)),
  );
  const failures = results.filter((r) => r.status === "rejected") as
    PromiseRejectedResult[];
  if (failures.length > 0) {
    throw new Error(failures.map((f) => String(f.reason)).join(" | "));
  }
  return "ok";
}

async function mark(
  table: string,
  id: number,
  fields: Record<string, unknown>,
): Promise<void> {
  await db.from(table).update({ ...fields, updated_at: new Date().toISOString() })
    .eq("id", id);
}

async function markDelivered(table: string, id: number): Promise<void> {
  await mark(table, id, {
    status: "delivered",
    delivered_at: new Date().toISOString(),
    last_error: null,
  });
}

async function markRetry(
  table: string,
  id: number,
  attempts: number,
  error: string,
): Promise<void> {
  const dead = attempts >= MAX_ATTEMPTS;
  const next = new Date(Date.now() + backoffSeconds(attempts) * 1000);
  await mark(table, id, {
    status: dead ? "failed" : "pending",
    last_error: error.slice(0, 2000),
    next_attempt_at: next.toISOString(),
  });
}

// ---- match outbox -----------------------------------------------------------

async function deliverMatch(row: MatchOutbox): Promise<void> {
  const { data: payload, error } = await db.rpc("build_match_event", {
    p_match_id: row.match_id,
  });
  if (error) throw new Error(`build_match_event: ${error.message}`);
  if (!payload) return markDelivered("delivery_outbox", row.id); // match deleted
  const r = await fanout(
    payload,
    row.competition_id,
    "match.update",
    row.match_id,
  );
  await markDelivered("delivery_outbox", row.id);
  if (r === "no-listeners") {/* nothing to do; don't pile up */}
}

// ---- entity outbox ----------------------------------------------------------

const ENTITY_EVENT: Record<EntityOutbox["entity_type"], string> = {
  competition: "CompetitionUpdated",
  team: "RosterUpdated",
  player: "PlayerUpdated",
  comp_sync: "CompetitionSyncCompleted",
};

async function buildEntity(row: EntityOutbox): Promise<unknown> {
  switch (row.entity_type) {
    case "competition": {
      const { data, error } = await db.rpc("build_competition_event", {
        p_competition_id: row.entity_id,
      });
      if (error) throw new Error(error.message);
      return data;
    }
    case "team": {
      const { data, error } = await db.rpc("build_team_event", {
        p_team_id: row.entity_id,
        p_competition_id: row.competition_id,
      });
      if (error) throw new Error(error.message);
      return data;
    }
    case "player": {
      const { data, error } = await db.rpc("build_player_event", {
        p_player_id: row.entity_id,
        p_competition_id: row.competition_id,
      });
      if (error) throw new Error(error.message);
      return data;
    }
    case "comp_sync": {
      const { data, error } = await db.rpc("build_comp_sync_event", {
        p_competition_id: row.entity_id,
      });
      if (error) throw new Error(error.message);
      return data;
    }
  }
}

async function deliverEntity(row: EntityOutbox): Promise<void> {
  const payload = await buildEntity(row);
  if (!payload) return markDelivered("entity_outbox", row.id); // entity gone
  await fanout(
    payload,
    row.competition_id,
    ENTITY_EVENT[row.entity_type],
    row.entity_id,
  );
  await markDelivered("entity_outbox", row.id);
}

// ---- drain both -------------------------------------------------------------

Deno.serve(async () => {
  let delivered = 0, retried = 0, claimed = 0;

  // Matches.
  const { data: mClaimed, error: mErr } = await db.rpc("claim_outbox_batch", {
    p_limit: BATCH,
  });
  if (mErr) return new Response(`claim error: ${mErr.message}`, { status: 500 });
  for (const row of (mClaimed ?? []) as MatchOutbox[]) {
    claimed++;
    try {
      await deliverMatch(row);
      delivered++;
    } catch (err) {
      retried++;
      await markRetry(
        "delivery_outbox",
        row.id,
        row.attempts,
        String((err as Error)?.message ?? err),
      );
    }
  }

  // Entities.
  const { data: eClaimed, error: eErr } = await db.rpc(
    "claim_entity_outbox_batch",
    { p_limit: BATCH },
  );
  if (eErr) {
    return new Response(`entity claim error: ${eErr.message}`, { status: 500 });
  }
  for (const row of (eClaimed ?? []) as EntityOutbox[]) {
    claimed++;
    try {
      await deliverEntity(row);
      delivered++;
    } catch (err) {
      retried++;
      await markRetry(
        "entity_outbox",
        row.id,
        row.attempts,
        String((err as Error)?.message ?? err),
      );
    }
  }

  return Response.json({ claimed, delivered, retried });
});
