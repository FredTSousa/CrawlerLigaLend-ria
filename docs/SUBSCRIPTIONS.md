# League subscriptions (change fan-out)

Lets an **external webpage** subscribe to a league and receive a webhook every
time a match in that league changes — kickoff/round/status info, live score and
minute, per-player stats, and A Bola reporter ratings. Works for one subscriber
or many; each keeps the data in its **own** Supabase (or any backend — it's just
a signed HTTPS POST).

## How it works

```
crawler write (round / live tick / reporter)         ── any path ──┐
  matches / match_players / matches_reporter_link                  │
        │  AFTER trigger                                           ▼
        └─────────────────────────────►  delivery_outbox  (one coalesced
                                          "this match is dirty" row per match)
                                                 │
              Database Webhook (instant) + pg_cron backstop (retries)
                                                 ▼
                                    dispatch  Edge Function
                                      • claim_outbox_batch()  (SKIP LOCKED)
                                      • build_match_event(match_id) → snapshot
                                      • find active subscribers for the league
                                      • HMAC-sign + POST to each callback_url
                                      • mark delivered / back-off retry
                                                 ▼
                                    subscriber's /ingest endpoint
                                      • verify X-Signature (HMAC-SHA256)
                                      • upsert into its own tables
```

Why triggers instead of emitting from Python: a match changes through **three**
code paths (`sync.write_games`, `live_watch.light_update`, and
`reporter_sync._store_and_link`), plus manual edits from the web app. A DB
trigger captures all of them with zero per-path code. The payload is built
**at send time** from current state, so coalesced bursts and retries always
deliver the latest snapshot — and the subscriber's upsert is idempotent, so a
re-delivery is a no-op.

## The payload

`POST` body (`Content-Type: application/json`), signed with
`X-Signature: sha256=<hex>` (HMAC-SHA256 of the raw body using the subscriber's
`secret`). Also sends `X-Event-Type: match.update` and `X-Match-Id`.

```jsonc
{
  "event": "match.update",
  "match_id": "11071716",
  "sent_at": "2026-05-31T18:32:05Z",
  "competition": { "id": "201241", "name": "...", "slug": "liga-portuguesa", "fase": "217930" },
  "match": {
    "id": "11071716", "round": 31, "played_on": "2026-04-25", "url": "...",
    "status": "live", "minute": "67'", "kickoff_at": "2026-04-25T19:30:00Z",
    "home_team": { "id": "1", "name": "Benfica" },
    "away_team": { "id": "20", "name": "Moreirense" },
    "home_score": 2, "away_score": 0,
    "scraped_at": "...", "updated_at": "..."
  },
  "players": [
    { "player_id": "...", "player_name": "...", "team_id": "1",
      "order_index": 0, "shirt_number": 7, "is_captain": false, "is_starter": true,
      "entered_min": null, "left_min": null,
      "goals": 1, "assists": 0, "yellow_cards": 0, "red_card": false,
      "own_goals": 0, "penalties_scored": 0, "penalties_missed": 0,
      "penalties_defended": 0, "played_under_20m": false,
      "reporter_score": 7, "reporter_is_mvp": true }
  ],
  "reporter": {            // null until A Bola ratings are fetched
    "fetched_at": "...", "format_detected": 1, "urls": [...],
    "home_ratings": [...], "away_ratings": [...]
  }
}
```

Each event is the **full current snapshot** of the match (not a diff), so the
subscriber just upserts the match and replaces its player rows.

## Setup — crawler side (this repo's Supabase)

1. Run `db/migration_subscriptions.sql` in the SQL editor.
2. Enable the `pg_net` and `pg_cron` extensions (Database → Extensions).
3. Deploy the dispatcher:
   ```bash
   supabase functions deploy dispatch --no-verify-jwt
   ```
4. Wire the triggers/cron at the bottom of `migration_subscriptions.sql`
   (replace `<PROJECT_REF>` / `<SERVICE_ROLE_KEY>`), **or** add a Database
   Webhook in the dashboard on `delivery_outbox` INSERT pointing at the
   `dispatch` function. The pg_cron backstop is recommended either way.
5. Register a subscriber:
   ```sql
   insert into public.subscribers (label, competition_id, callback_url, secret)
   values ('fantasy-site', '201241',
           'https://<their-ref>.functions.supabase.co/ingest',
           '<a long random shared secret>');
   ```
   `competition_id` is the league (`competitions.id` / `id_edicao`). Use `NULL`
   to receive **every** league.

## Setup — subscriber side (their Supabase)

1. Run `db/subscriber_schema.example.sql` (adapt the columns you want).
2. Copy `supabase/functions/ingest-example/` into their project, set the secret:
   ```bash
   supabase secrets set CRAWLER_WEBHOOK_SECRET=<same secret as above>
   supabase functions deploy ingest-example --no-verify-jwt
   ```
3. Give the crawler admin that function's URL to put in `subscribers.callback_url`.

> Multiple subscribers: just add more `subscribers` rows (each with its own
> `secret` and `callback_url`). The dispatcher fans out to all active rows for
> the league and retries the event until every one returns 2xx; because ingest
> is idempotent, re-sends to already-delivered subscribers are harmless.

## Operating notes

- **Backfill / replay:** `update delivery_outbox set status='pending',
  attempts=0, next_attempt_at=now() where match_id='...';` re-sends a match.
  To seed a brand-new subscriber with a whole league, `insert ... select` one
  pending outbox row per match in that competition.
- **Inspect:** `select status, count(*) from delivery_outbox group by 1;` and
  `select * from delivery_outbox where status='failed';` (the `last_error`
  column holds the subscriber's response).
- **Latency:** live ticks arrive within the webhook round-trip; the 30s
  pg_cron backstop only matters if the webhook is missing or a retry is due.
- **Security:** subscribers never touch this DB. They only receive signed POSTs
  and verify the HMAC; reject any request whose `X-Signature` doesn't match.
```
