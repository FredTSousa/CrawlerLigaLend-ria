# Competition Management Backoffice — Technical Design

> Extends the existing fixture-only model so a **Competition** owns **Teams** and
> **Players**, with a backoffice to manage and sync them and an export path to
> subscribers. Designed to fit the system as it already is: zerozero IDs as
> natural keys, `crawler.py`→`sync.py`→Supabase, the `delivery_outbox`/`dispatch`
> fan-out, and the Next.js admin in `web/`.

---

## 0. Grounding: how the system works today (and what that forces)

| Layer | Today | Implication for this design |
|-------|-------|-----------------------------|
| Scrape | `crawler.py`, 3 composable stages, `curl_cffi` TLS impersonation | New roster/player scraping is a 4th/5th stage in the same style |
| Persist | `sync.py` UPSERTs via PostgREST, **zerozero id = text PK** | Teams/players keep their zerozero id as PK; new tables follow suit |
| Run/trigger | web `POST /api/crawl` → queued `crawl_runs` row → GitHub Actions self-hosted runner (residential IP) → `sync.py` | New sync kinds reuse this exact path; no new infra |
| Export | DB trigger → coalesced `delivery_outbox` → `dispatch` Edge Function → HMAC POST | Extend the same outbox, add entity events |
| Admin | Next.js `web/`, Supabase RLS gated by `allowed_users` | New pages reuse RLS + the `crawl_runs` panel pattern |

**Key realization — season is already modeled.** `competitions.id` holds
`id_edicao`, which is per-season. The same league across seasons is already two
rows sharing `slug`. So *Competition = Competition-Season*. We do **not** need a
separate `CompetitionSeason` entity; we need (a) explicit season metadata on the
competition row, (b) a season-scoped **membership** join, and (c) a season-scoped
**roster** join. Teams and players stay global (their zerozero ids are stable).

**The other key fact — `epoca_id` ≠ `id_edicao`.** The squad page is
`/equipa/santa-clara/32?epoca_id=155`. `155` is the *season* id; `id_edicao` is
the *edition* id. We must store `epoca_id` on the competition so the roster
importer can address each team's season page.

---

## 1. Database schema

All additions are **additive and nullable** — nothing existing breaks. New tables
follow the existing convention (zerozero id as `text` PK, `updated_at default now()`,
RLS read-gated by `is_allowed()`, service_role writes).

```sql
-- ============================================================================
-- migration_competition_squads.sql  (additive; safe to re-run)
-- ============================================================================

-- 1) Competition: enrich the existing row (do NOT recreate it).
alter table public.competitions
    add column if not exists full_name    text,        -- "Liga Portugal Betclic 2025/26"
    add column if not exists season        text,        -- "2025/26"
    add column if not exists epoca_id       text,        -- season id used in /equipa/<id>?epoca_id=
    add column if not exists source_url     text,        -- /competicao/<slug>
    add column if not exists last_sync_at   timestamptz,
    add column if not exists created_at     timestamptz not null default now();
-- NOTE: competitions.id already == zerozero id_edicao (the zerozero competition
-- identifier). We keep it as the PK; `zerozero_id` would be a redundant alias so
-- we do not add one. `slug` already groups a league across seasons.

-- 2) Teams: enrich the existing thin row.
alter table public.teams
    add column if not exists slug         text,         -- "santa-clara"
    add column if not exists logo_url     text,
    add column if not exists source_url   text,         -- /equipa/<slug>/<id>
    add column if not exists last_sync_at timestamptz,
    add column if not exists created_at   timestamptz not null default now();

-- 3) Players: enrich the existing thin row with STABLE attributes + latest snapshot.
--    Season-varying values (age at time, club, roster group) live on the roster row;
--    these player columns hold the latest authoritative values for convenience.
alter table public.players
    add column if not exists slug           text,
    add column if not exists birth_date     date,        -- stable; preferred over age
    add column if not exists age            int,         -- latest scraped age (derived/volatile)
    add column if not exists nationality    text,
    add column if not exists position       text,        -- detailed "Posição" (authoritative)
    add column if not exists position_group text,        -- latest roster grouping
    add column if not exists position_code  text,        -- normalized: GK, CB, ... (see §6)
    add column if not exists club_name      text,        -- latest club
    add column if not exists source_url     text,        -- /jogador/<slug>/<id>
    add column if not exists last_sync_at   timestamptz,
    add column if not exists enriched_at    timestamptz, -- last time the detail page was opened
    add column if not exists created_at     timestamptz not null default now();

-- 4) Competition ↔ Team membership (SEASON-SCOPED because competition_id is per-season).
--    "Santa Clara in Liga Portugal 2025/26" is one row; the 2024/25 edition is another.
create table if not exists public.competition_teams (
    competition_id text not null references public.competitions(id) on delete cascade,
    team_id        text not null references public.teams(id),
    source_url     text,                       -- the team's season page used to crawl
    active         boolean not null default true,  -- false = no longer in this edition
    first_seen_at  timestamptz not null default now(),
    last_sync_at   timestamptz,
    updated_at     timestamptz not null default now(),
    primary key (competition_id, team_id)
);
create index if not exists competition_teams_team_idx on public.competition_teams (team_id);

-- 5) Roster membership (player ∈ team ∈ competition-season). The historical record.
--    position_group is AUTHORITATIVE from the roster grouping (per season).
--    We NEVER hard-delete: a departed player gets active=false + left_at.
create table if not exists public.roster_memberships (
    competition_id text not null references public.competitions(id) on delete cascade,
    team_id        text not null references public.teams(id),
    player_id      text not null references public.players(id),
    position_group text,                       -- "Guarda Redes" | "Defesa" | "Médio" | "Avançado"
    shirt_number   int,
    age_at_sync    int,                         -- snapshot of age when crawled
    active         boolean not null default true,
    first_seen_at  timestamptz not null default now(),
    left_at        timestamptz,                 -- set when player drops out of the roster
    last_sync_at   timestamptz,
    updated_at     timestamptz not null default now(),
    primary key (competition_id, team_id, player_id)
);
create index if not exists roster_player_idx on public.roster_memberships (player_id);
create index if not exists roster_comp_idx   on public.roster_memberships (competition_id);
create index if not exists roster_team_idx   on public.roster_memberships (team_id);

-- 6) Sync run log: reuse crawl_runs. New `kind` values:
--    'comp' | 'teams' | 'roster' | 'players' | 'comp_full'
--    (No schema change needed — kind/target/source are free-text already.)

-- 7) RLS: read for allow-listed users; service_role writes (bypasses RLS).
alter table public.competition_teams   enable row level security;
alter table public.roster_memberships  enable row level security;
do $$ declare t text; begin
  foreach t in array array['competition_teams','roster_memberships'] loop
    execute format('drop policy if exists %I on public.%I', t||'_read', t);
    execute format('create policy %I on public.%I for select to authenticated using (public.is_allowed())', t||'_read', t);
  end loop;
end $$;
```

### Why this shape

- **No `CompetitionSeason` entity.** It would duplicate what `id_edicao` already
  encodes and break the existing `matches.competition_id` / `subscribers.competition_id`
  FKs. Season metadata goes *on* the competition row; cross-season navigation uses
  `slug`.
- **`competition_teams` is the membership; it is season-scoped for free** because
  its `competition_id` is a season-edition. Santa Clara 24/25 vs 25/26 are
  distinct `(competition_id, team_id)` rows.
- **`roster_memberships` is the historical roster.** Soft-delete only (`active`,
  `left_at`) → history is never lost. This is the table that answers "who was in
  the squad that season."
- **`position_group` lives on the roster row** (it is read per-season from the
  roster grouping and *is* season-specific). `position`/`position_code` live on
  the player (the detailed value, mostly stable) **and** we also stamp the group
  onto the player as a convenience "latest" copy. Authoritative-for-history =
  roster row; authoritative-for-current = player row.

---

## 2. Entity-Relationship diagram

```
                         ┌───────────────────────┐
                         │     competitions       │  (id = id_edicao, per SEASON)
                         │  id, slug, season,     │
                         │  epoca_id, fase, ...    │
                         └───────────┬────────────┘
            slug groups seasons      │ 1
        (liga-portuguesa 24/25,      │
         25/26, ...)                  │
                 ┌────────────────────┼─────────────────────┐
                 │ N                  │ N                    │ N
        ┌────────▼─────────┐ ┌────────▼──────────┐  ┌────────▼────────┐
        │ competition_teams│ │ roster_memberships│  │     matches      │
        │ (comp, team)     │ │ (comp,team,player)│  │ (existing)       │
        │ membership       │ │ position_group,   │  │ home/away_team   │
        └────────┬─────────┘ │ active, left_at   │  └──────────────────┘
                 │ N         └───┬───────────┬───┘
                 │ 1            N│          N │
            ┌────▼────┐    ┌─────▼───┐   ┌────▼─────┐
            │  teams   │◄──┤ (team)  │   │ players  │
            │ global   │   └─────────┘   │ global   │
            │ id stable│                 │ id stable│
            └──────────┘                 │ position │
                                         │ pos_code │
                                         └──────────┘
        match_players (existing) ── player_id, team_id, match_id ── unchanged
```

- `teams` and `players` are **global, deduplicated** (stable zerozero ids).
- A player's *participation history* already exists via `match_players`; the new
  `roster_memberships` adds *squad membership* history (broader than appearances).

---

## 3. Recommended architecture

Keep the three-tier split that already works:

```
┌──────────────┐   scrape (curl_cffi)   ┌──────────────┐   PostgREST UPSERT   ┌──────────┐
│  roster.py    │ ─────────────────────► │   sync.py     │ ───────────────────► │ Supabase │
│ (new stages)  │                        │ (new writers) │                      │ Postgres │
└──────────────┘                        └──────────────┘                      └────┬─────┘
        ▲                                       ▲                                   │ triggers
        │ dispatched by GitHub Actions          │                                   ▼
        │ (self-hosted, residential IP)         │                          entity_outbox / delivery_outbox
        │                                       │                                   │
┌───────┴────────┐  POST /api/crawl   ┌─────────┴────────┐                          ▼
│  web/ backoffice│ ─────────────────► │  crawl_runs row  │                  dispatch Edge Function
│ (Next.js)       │                    │  + workflow_dispatch                 → HMAC POST subscribers
└────────────────┘                    └──────────────────┘
```

**New scrape module `roster.py`** mirrors `crawler.py`'s composable stages:

```python
import roster
teams   = roster.get_competition_teams(comp)        # stage A: discover teams in a season
squad   = roster.get_team_roster(comp, team)        # stage B: roster grouped by position_group
player  = roster.get_player_detail(player_id)       # stage C: detailed "Posição", age, club
```

`get_competition_teams` reads the competition standings/teams listing and the
`epoca_id`; `get_team_roster` parses the `/equipa/<id>?epoca_id=<season>` page,
walking the four `Guarda Redes / Defesa / Médio / Avançado` group headers exactly
like `parse_lineups` walks the `subtitle` markers; `get_player_detail` opens the
player page and reads the "Posição" row.

**New `sync.py` writers** mirror `write_games`: `write_teams`, `write_roster`,
`write_player_details` — each builds rows and UPSERTs in FK-safe order
(`competitions → teams → competition_teams → players → roster_memberships`).

---

## 4. Backoffice UI design (Next.js, in `web/`)

Reuse existing patterns: server components + Supabase RLS reads, the `crawl_runs`
panel (`RunsPanel.tsx`), and `POST /api/crawl` for actions.

### Routes
```
/competitions                      list (existing competitions, now with counts)
/competitions/[id]                 details page with tabs
/competitions/[id]/teams/[teamId]  team detail (roster)
/players/[id]                      player detail + history
```

### `/competitions/[id]` — wireframe

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Liga Portugal Betclic — 2025/26                       [Sync ▾] [Full Refresh]│
│  comp 201241 · zz 201241 · slug liga-portuguesa · época 155                   │
├──────────┬──────────┬──────────┬───────────────────────────────────────────┤
│ Teams    │ Players  │ Fixtures │ Last sync: 2026-06-02 14:03 (2h ago)         │
│   18     │   543    │   306    │                                              │
├──────────┴──────────┴──────────┴───────────────────────────────────────────┤
│ [ Teams ] [ Players ] [ Fixtures ] [ Sync history ] [ Errors ]               │
├──────────────────────────────────────────────────────────────────────────────
│  TEAMS TAB                                                                    │
│  ┌────┬──────────────┬───────┬─────────┬──────────────┬───────────────────┐  │
│  │logo│ Name          │ zz id │ Players │ Last sync     │ actions           │  │
│  ├────┼──────────────┼───────┼─────────┼──────────────┼───────────────────┤  │
│  │ ⚽ │ Santa Clara   │ 32    │  27     │ 2h ago        │ View · Refresh · ↗ │  │
│  │ ⚽ │ Benfica       │ 1     │  31     │ 2h ago        │ View · Refresh · ↗ │  │
│  └────┴──────────────┴───────┴─────────┴──────────────┴───────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

- **Summary cards**: Teams / Players / Fixtures / Last sync (the four counts + timestamp).
- **`[Sync ▾]`** dropdown: *Sync Competition*, *Sync Teams* (fast), *Sync Players* (full), *Full Refresh* — each POSTs `/api/crawl` with a new `kind` (§7) and shows a toast linking to the new `crawl_runs` row.
- **Teams tab**: table per the requirements; row actions *View / Refresh roster / Open source ↗*.
- **Players tab**: server-filtered table (Team, Position Group, Position, Name search) over `roster_memberships ⨝ players`; columns Name, Age, Group, Position, Team, Club, zz id, Last updated.
- **Fixtures tab**: reuse the existing round/match components unchanged — query `matches` by `competition_id`.
- **Sync history**: filtered `crawl_runs` (the existing panel, `kind in ('comp','teams','roster','players','comp_full')`).
- **Errors tab**: `crawl_runs` rows with `status='error'` + the `error` text, plus per-player enrichment failures (a `last_error` we add to the roster/player sync log).

---

## 5. Import workflow diagrams

### 5.1 Competition import (discover teams)
```
get_competition(slug)                    # reuse crawler.get_competition → id_edicao, fase, name
        │  + read epoca_id, season label
        ▼
get_competition_teams(comp)              # NEW: list teams in the edition
        │  -> [{team_id, name, slug, logo_url, source_url(epoca_id)}]
        ▼
UPSERT competitions (season, epoca_id, full_name, source_url, last_sync_at)
UPSERT teams         (name, slug, logo_url, source_url)
UPSERT competition_teams (competition_id, team_id, active=true)
        │
        ▼
reconcile: competition_teams not seen this run → active=false  (team left the league)
```

### 5.2 Team roster import (per team)
```
for team in competition_teams(comp):
    html = fetch(/equipa/<team_id>?epoca_id=<comp.epoca_id>)
    for group in [Guarda Redes, Defesa, Médio, Avançado]:        # parse group headers
        for player in group:
            UPSERT players (id, name, slug)                       # thin create
            UPSERT roster_memberships (comp, team, player,
                                       position_group=group, shirt_number, active=true)
    reconcile: roster rows for this team not seen → active=false, left_at=now()
```

### 5.3 Player enrichment (full sync only)
```
for player where enriched_at is null OR stale:                   # bounded worklist
    html = fetch(/jogador/<slug>/<player_id>)
    extract full_name, age/birth_date, position("Posição"), club, nationality
    UPDATE players (position, position_code=map(position), age, club_name,
                    nationality, birth_date, enriched_at=now())
    # polite delay between requests; this is the expensive, rate-limited stage
```

### 5.4 Fast vs Full
| Mode | Steps | Requests (≈18-team league) | Use |
|------|-------|----------------------------|-----|
| **Fast Sync** | 5.1 + 5.2 | 1 + ~18 | Frequent. Squad membership & groups, no detail pages |
| **Full Sync** | 5.1 + 5.2 + 5.3 | 1 + ~18 + ~550 | Occasional. Adds detailed position/age/club |

Fast Sync gives `position_group` (from the roster grouping) immediately;
`position`/`position_code` populate only after Full Sync opens each player page.

---

## 6. Position normalization (`position_code`)

`position_group` (roster page) → coarse; `position` (player "Posição") → authoritative;
`position_code` → the export-stable enum downstream systems key on.

| position (PT, authoritative) | group | position_code |
|------------------------------|-------|---------------|
| Guarda-Redes | Guarda Redes | `GK` |
| Defesa Central | Defesa | `CB` |
| Defesa Direito | Defesa | `RB` |
| Defesa Esquerdo | Defesa | `LB` |
| Ala Direito | Defesa/Médio | `RWB` |
| Ala Esquerdo | Defesa/Médio | `LWB` |
| Médio Defensivo | Médio | `CDM` |
| Médio Centro | Médio | `CM` |
| Médio Ofensivo | Médio | `CAM` |
| Extremo Direito | Avançado | `RW` |
| Extremo Esquerdo | Avançado | `LW` |
| Segundo Avançado | Avançado | `SS` |
| Ponta de Lança | Avançado | `ST` |

Implementation: a single source-of-truth dict `POSITION_CODES` in `roster.py`,
applied at write time. Unknown positions are stored verbatim with
`position_code = NULL` **and logged to stderr** (exactly like the
`? unmapped events` pattern in `crawler.py`) so the vocabulary can be extended.
Keep the map data-driven — never let an unmapped position drop a player.

---

## 7. Synchronization strategy

| Concern | Rule |
|---------|------|
| **Incremental (Fast)** | Re-run 5.1+5.2; UPSERT by natural key. Cheap; safe to run every few hours via schedule alongside the existing fixture cron |
| **Full refresh** | 5.1+5.2+5.3 with `force` (re-enrich every player, ignore `enriched_at`) |
| **Change detection** | UPSERT is idempotent. Compare scraped vs stored on the writer side; only bump `updated_at`/enqueue an event when a meaningful field changed (mirror `trg_matches_enqueue`'s "nothing a subscriber cares about changed → skip") |
| **Transferred player** | New club's roster sync creates a new `roster_memberships` row; the old club's reconcile sets its row `active=false, left_at=now()`. Both rows persist → history intact. `players.club_name` updates to the latest |
| **Released player** | Reconcile sets `active=false, left_at` on the old roster row; player row stays |
| **Retired player** | Same as released; never appears in any new roster. The `players` row remains for historical `match_players`/roster joins |
| **Historical preservation** | Soft-delete only. No DELETEs on `players`, `teams`, `roster_memberships`, `competition_teams`. Because `competition_id` is per-season, each season's roster is a distinct, frozen set of rows |
| **Season rollover** | A new edition = a new `competitions` row (new `id_edicao`) discovered from the same `slug`. Run Competition import against the new edition; last season's rows are untouched. No data migration |

**Reconcile = the key primitive.** After each Fast Sync, any membership/roster row
in the DB for that competition that was *not seen* in this crawl is flipped
`active=false` (not deleted). Newly seen rows are inserted/reactivated.

---

## 8. API design

Thin Next.js route handlers over Supabase (RLS-gated reads) for queries;
`POST /api/crawl` (extended) for sync actions. Read endpoints can also be served
directly by PostgREST views if a subscriber prefers pull (§9 Option B).

### Competitions
```
GET  /competitions                      ?slug=&season=        list + counts
GET  /competitions/{id}                                       metadata + counts + last_sync_at
GET  /competitions/{id}/teams                                 competition_teams ⨝ teams
GET  /competitions/{id}/players          ?team=&group=&position=&q=&page=
GET  /competitions/{id}/fixtures                              matches by competition_id (existing)
POST /competitions/{id}/sync             {scope:"teams"|"roster"}   Fast Sync (kind=teams|roster)
POST /competitions/{id}/full-sync                                   Full Sync (kind=comp_full)
```
### Teams
```
GET  /teams                              ?q=
GET  /teams/{id}                                              + seasons it appears in
GET  /teams/{id}/players                 ?competition=        roster (defaults to latest season)
POST /teams/{id}/sync                    {competition_id}     refresh one team's roster (kind=roster)
```
### Players
```
GET  /players                            ?q=&position_code=&team=&competition=
GET  /players/{id}                                            current snapshot
GET  /players/{id}/history                                    all roster_memberships (season,team,group)
                                                              + optional appearance summary from match_players
POST /players/{id}/refresh                                    re-enrich one player (kind=players)
```

`POST /competitions/{id}/sync` etc. all funnel into the existing
`POST /api/crawl` mechanism: insert a queued `crawl_runs` row, dispatch the
workflow with the new `kind` + `competition`/`team`/`player` target, and return
the `run_id` for the UI to poll — identical to the fixture flow.

---

## 9. Export architecture

The system already has a robust, battle-tested fan-out (`delivery_outbox` →
`dispatch` → HMAC POST). **Extend it; don't replace it.** Three options were
requested — the recommendation is a **hybrid (A primary, C for bulk, B optional)**.

### Option A — Event-driven (extend the existing outbox)  ✅ primary

Add an **entity outbox** parallel to `delivery_outbox`, plus `build_*_event`
functions and triggers, mirroring the match flow exactly.

```sql
create table if not exists public.entity_outbox (
    id              bigint generated always as identity primary key,
    entity_type     text not null,              -- 'competition'|'team'|'player'|'roster'
    entity_id       text not null,              -- competition_id / team_id / player_id
    competition_id  text,                        -- routing key (NULL = all)
    status          text not null default 'pending',
    attempts        int not null default 0,
    last_error      text,
    next_attempt_at timestamptz not null default now(),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    delivered_at    timestamptz
);
create unique index if not exists entity_outbox_pending_uq
    on public.entity_outbox (entity_type, entity_id) where status='pending';
```

Triggers on `competition_teams` and `roster_memberships` enqueue
`RosterUpdated`/`TeamUpdated`; on `players` enqueue `PlayerUpdated`; a
`CompetitionSyncCompleted` event is enqueued by `sync.py` when a run finishes.
The `dispatch` function gains `build_competition_event`/`build_team_event`/
`build_player_event` and routes by `competition_id` to the same `subscribers`.

Events: `CompetitionUpdated`, `TeamUpdated`, `PlayerUpdated`, `RosterUpdated`,
`CompetitionSyncCompleted`.

- **Pros**: reuses proven coalescing + SKIP-LOCKED claim + backoff + HMAC; low
  latency; subscribers stay idempotent-upsert; one signing scheme. Zero new infra.
- **Cons**: more event types to version; per-player churn on a full sync could
  enqueue ~550 events (mitigated by coalescing + emit-on-change-only).
- **Scalability**: same as today's match path — coalescing collapses bursts; the
  pg_cron backstop drains retries. Fine at hundreds of competitions.
- **Reliability**: at-least-once with retry + dead-letter (`status='failed'`),
  identical to the current guarantees.

### Option B — Pull APIs

Expose the §8 read endpoints (or PostgREST views) for subscribers to poll.

- **Pros**: dead simple; no delivery state; subscriber controls cadence; great for
  reconciliation/audit and for new subscribers doing an initial load.
- **Cons**: polling latency + wasted requests; subscribers must diff; auth/rate-limit
  to manage. Not real-time.
- **Scalability**: bounded by read replicas + caching.
- **Caching**: `ETag`/`Last-Modified` from `max(updated_at)` per competition;
  `Cache-Control` short TTL; a cheap `GET /competitions/{id}?fields=counts,last_sync_at`
  as a change probe so pollers fetch the heavy payload only when it moved.

### Option C — Snapshot export

A single signed package per competition-season (the `replay_competition` analog
for entities): `GET /competitions/{id}/export` → one JSON document (competition +
teams + roster/players + fixtures), versioned.

- **Pros**: perfect for onboarding a new subscriber, backfills, offline diffing,
  and reproducibility; one atomic artifact.
- **Cons**: not incremental; large; can go stale between builds.
- **Versioning**: include `schema_version` + `generated_at` + a content `hash`;
  store snapshots in Supabase Storage keyed `comp/{id}/{generated_at}.json`; keep
  N latest. Subscribers compare `hash` to skip no-op imports.

### Recommendation
- **A** is the steady-state channel (extends what exists, real-time, reliable).
- **C** seeds new subscribers and supports bulk reconcile — the entity analog of
  the existing `replay_competition` re-send.
- **B** is an optional convenience/audit surface.
Emit-on-change (don't enqueue when nothing meaningful changed) keeps A's volume
sane during full syncs.

---

## 10. Example JSON payloads

### Competition snapshot (Option C export, and `GET /competitions/{id}`)
```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-06-02T14:03:00Z",
  "competition": {
    "id": "201241", "zerozero_id": "201241",
    "full_name": "Liga Portugal Betclic 2025/26", "season": "2025/26",
    "slug": "liga-portuguesa", "fase": "217930", "epoca_id": "155",
    "source_url": "https://www.zerozero.pt/competicao/liga-portuguesa",
    "last_sync_at": "2026-06-02T14:03:00Z",
    "counts": { "teams": 18, "players": 543, "fixtures": 306 }
  },
  "teams": [
    { "id": "32", "name": "Santa Clara", "slug": "santa-clara",
      "logo_url": "https://.../logos/equipas/32_...png",
      "source_url": "https://www.zerozero.pt/equipa/santa-clara/32?epoca_id=155",
      "player_count": 27, "last_sync_at": "2026-06-02T14:03:00Z" }
  ],
  "players": [
    { "id": "445566", "name": "Player Name", "slug": "player-name",
      "age": 24, "birth_date": "2001-08-14", "nationality": "Portugal",
      "position_group": "Médio", "position": "Médio Ofensivo", "position_code": "CAM",
      "team_id": "32", "club_name": "Santa Clara",
      "shirt_number": 10, "active": true,
      "source_url": "https://www.zerozero.pt/jogador/player-name/445566",
      "last_updated": "2026-06-02T14:03:00Z" }
  ]
}
```

### `GET /players/{id}/history`
```jsonc
{
  "player": { "id": "445566", "name": "Player Name", "position_code": "CAM" },
  "memberships": [
    { "competition_id": "201241", "season": "2025/26", "team_id": "32",
      "team_name": "Santa Clara", "position_group": "Médio",
      "shirt_number": 10, "active": true,  "first_seen_at": "2025-07-30T...", "left_at": null },
    { "competition_id": "198877", "season": "2024/25", "team_id": "9",
      "team_name": "Vitória SC", "position_group": "Médio",
      "shirt_number": 8, "active": false, "first_seen_at": "2024-08-01T...", "left_at": "2025-07-01T..." }
  ]
}
```

### Webhook events (Option A — same envelope/HMAC as `match.update`)
```jsonc
// X-Event-Type: roster.update
{ "event": "RosterUpdated", "sent_at": "2026-06-02T14:03:05Z",
  "competition": { "id": "201241", "slug": "liga-portuguesa", "season": "2025/26" },
  "team": { "id": "32", "name": "Santa Clara" },
  "roster": [
    { "player_id": "445566", "player_name": "Player Name", "position_group": "Médio",
      "position": "Médio Ofensivo", "position_code": "CAM", "shirt_number": 10, "active": true } ] }

// X-Event-Type: player.update
{ "event": "PlayerUpdated", "sent_at": "...",
  "competition": { "id": "201241", "slug": "liga-portuguesa" },
  "player": { "id":"445566","name":"Player Name","age":24,"position":"Médio Ofensivo",
              "position_code":"CAM","position_group":"Médio","club_name":"Santa Clara","team_id":"32" } }

// X-Event-Type: competition.sync_completed
{ "event": "CompetitionSyncCompleted", "sent_at": "...",
  "competition": { "id":"201241","slug":"liga-portuguesa","season":"2025/26" },
  "run": { "id": 812, "kind": "comp_full", "teams": 18, "players": 543,
           "enriched": 543, "status": "success" } }
```

---

## 11. Migration plan (from fixture-only)

Phased, additive, reversible — no downtime, no break to fixtures or subscribers.

1. **Schema (additive).** Apply `migration_competition_squads.sql`. All new columns
   nullable; new tables empty. Existing fixture flow untouched.
2. **Backfill competition metadata.** For each existing `competitions` row, run the
   Competition import once to fill `season`, `epoca_id`, `full_name`, `source_url`.
   Teams already exist (from match crawls); membership is derived: seed
   `competition_teams` from the distinct `home_team_id`/`away_team_id` already in
   `matches` per competition, then a Fast Sync corrects/extends it.
3. **First Fast Sync per active competition.** Populates `competition_teams` +
   `roster_memberships` + `position_group`. League is now "squad-aware."
4. **First Full Sync (off-peak).** Enriches players (detailed position, age, club).
   Rate-limited; run per competition, not all at once.
5. **Export extension.** Add `entity_outbox` + `build_*_event` + triggers + the
   `dispatch` cases. Ship dark first (no entity subscribers) to validate volume,
   then enable.
6. **Backoffice.** Ship `/competitions/[id]` read-only (tabs + counts), then wire
   the Sync actions, then the history/errors tabs.

Rollback at any step: new tables/columns are inert; dropping the new `dispatch`
cases reverts export to fixtures-only.

---

## 12. Scalability considerations

Target: hundreds of competitions, thousands of teams, tens of thousands of players,
multiple seasons.

- **Crawl is the bottleneck, not the DB.** zerozero rate-limits and needs the
  residential self-hosted runner. Full Sync ≈ (teams + players) requests per
  competition. Mitigations: Fast Sync as the default cadence; enrichment worklist
  bounded by `enriched_at` staleness; per-competition jobs with `concurrency`
  groups (extend the existing `concurrency: crawl-sync` to be per-competition so
  leagues sync in parallel without overlapping themselves); polite `--delay`.
- **DB volume is modest.** ~tens of thousands of player rows + roster rows per
  season is small for Postgres. Indexes on `roster_memberships(player_id)`,
  `(competition_id)`, `(team_id)` cover the backoffice filters. Player-list
  pagination (keyset on `name`/`id`).
- **Export volume.** Coalescing + emit-on-change keep `entity_outbox` small even
  on full syncs; `CompetitionSyncCompleted` lets subscribers do one bulk pull
  instead of reacting to 550 `PlayerUpdated` events (offer both).
- **Read load.** Counts (`teams`/`players`/`fixtures`) via a cached
  `competition_counts` view or stamped onto the competition row at sync time to
  avoid `count(*)` on every page load.

---

## 13. Recommended implementation phases

| Phase | Deliverable | Notes |
|-------|-------------|-------|
| **P0** | `migration_competition_squads.sql` | Additive schema only |
| **P1** | `roster.py` stages A/B + `sync.write_teams`/`write_roster` + new `crawl_runs` kinds + workflow inputs | Fast Sync works end-to-end via CLI |
| **P2** | `/api/crawl` extension + `/competitions` & `/competitions/[id]` (Teams/Players/Fixtures tabs, counts, Sync buttons) | Backoffice read + Fast Sync actions |
| **P3** | `roster.py` stage C + `write_player_details` + Full Sync + position normalization | Detailed positions/age/club |
| **P4** | `entity_outbox` + `build_*_event` + triggers + `dispatch` cases (Option A) | Event export, shipped dark then enabled |
| **P5** | Snapshot export (Option C) + `/competitions/{id}/export` + Storage versioning | New-subscriber seeding & reconcile |
| **P6** | Sync history + Errors tabs, per-competition concurrency, counts caching, pull APIs (Option B) | Hardening & operability |

---

## 14. Risks & edge cases

- **`epoca_id` discovery.** The roster importer depends on the season id. If it's
  not on the competition page, derive it from a team link's `?epoca_id=` on that
  page. Store it on the competition; fail loudly if missing (don't crawl the wrong
  season silently).
- **`position_group` vs `position` mismatch.** "Ala" can map to defense or midfield
  depending on system; treat group as a hint, `position` as authoritative, and
  keep `POSITION_CODES` data-driven with stderr logging for unmapped values (the
  `? unmapped events` precedent).
- **Same player, two clubs mid-season (loan/transfer).** Both `roster_memberships`
  rows can be `active` briefly. Reconcile per-team (only deactivate rows for the
  team being synced) so a transfer doesn't wrongly deactivate the new club's row.
- **Squad page shows players who never appear in `match_players`** (and vice-versa).
  These are different relations on purpose — membership ≠ appearance. The UI should
  not assume one implies the other.
- **Encoding.** Player names carry the same Windows-1252 accents (`Trincão`) the
  crawler already handles — reuse `crawler.clean_name`/the response-encoding logic;
  don't force UTF-8.
- **Anti-bot / 403.** Player enrichment multiplies request count → higher block
  risk. Keep `curl_cffi` impersonation, polite delays, retries, and run enrichment
  off-peak in bounded batches.
- **Big-club id quirks.** As with crests in fixtures, team/player ids may be in
  image filenames rather than links — reuse the "read id from the logo URL" trick.
- **Event storms on full sync.** Without emit-on-change + coalescing, a full sync
  could flood subscribers. Gate enqueue on a real field delta and prefer
  `CompetitionSyncCompleted` + bulk pull for big changes.
- **Season rollover races.** A new edition appearing mid-crawl should never write
  into the old edition's rows — key everything by the resolved `competition_id`
  for the run, captured once at the start (like `crawl_round` resolves `comp` once).

---

## Final recommendation

**Model the competition as the season-edition it already is** (`competitions.id =
id_edicao`), keep **teams and players global** with stable zerozero ids, and put
all season-specific truth in two soft-deleted join tables — **`competition_teams`**
(membership) and **`roster_memberships`** (squad, with `position_group` and
history). This preserves every season distinctly with zero migration at rollover,
keeps the FKs that fixtures and subscribers already rely on, and matches the
natural-key/UPSERT idiom the codebase is built on.

For sync, **mirror the fixture pipeline** (`roster.py` stages + `sync.py` writers +
`crawl_runs` + the existing GitHub-Actions trigger), with **Fast Sync as the
default cadence and Full Sync occasional** to respect zerozero's rate limits.

For export, **extend the existing event-driven outbox** (Option A) as the primary
channel, add a **versioned snapshot** (Option C) for onboarding/reconcile, and
expose **pull APIs** (Option B) as a convenience — emitting on real change only.

This is the most maintainable path because it adds *no new architectural concepts*:
every piece is the squad-shaped twin of something already running in production.
