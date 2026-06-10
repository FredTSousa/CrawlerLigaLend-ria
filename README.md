# CrawlerLigaLendária

A web crawler for [zerozero.pt](https://www.zerozero.pt/) that collects Liga
Portugal results and per-player statistics, **fixture by fixture, game by
game**, through three small composable functions.

For each game it records **only the players who actually played** (starters +
substitutes who came on — the greyed-out unused substitutes are excluded).
Teams and players are emitted as `{ "name": ..., "id": ... }`.

## The pipeline (3 functions)

```python
import crawler

# 1. From the competition landing page, get the variables to request fixtures.
comp = crawler.get_competition()
#   -> {name, url, id_edicao, fase, rounds: [1..34], current_round}

# 2. From a fixture (round number), get all matches + the link to each.
fixture = crawler.get_fixture(comp, 31)
#   -> {round, url, games: [{game_id, slug, url, date,
#                            home_team{name,id}, away_team{name,id}, result}]}

# 3. From a match, get the JSON with the data.
for game in fixture["games"]:
    data = crawler.get_match(game)      # accepts the game dict ...
#   data = crawler.get_match(game["url"])  # ... or just the match URL
```

Each stage is independent: `get_match` works from a bare match URL (it reads
the teams/result from the match page), and `get_fixture` accepts either the
`comp` dict or a bare `fase` value. A `crawl_round(...)` helper composes all
three for one round.

## What it extracts

Per game: `home_team`, `away_team`, final `result` (`{home, away}`), date, URL.

Per player who played: `team`, shirt `number`, `captain`, `starter`, the
`minutes` they entered/left, and a `stats` block:

| stat | meaning |
|------|---------|
| `goals` | open-play goals (excludes penalties and own goals) |
| `assists` | assists |
| `yellow_cards` | yellow cards shown |
| `red_card` | `true` if sent off (straight red **or** second yellow) |
| `own_goals` | own goals |
| `penalties_scored` | penalties converted (counted separately — a player's total goals = `goals + penalties_scored`) |
| `penalties_missed` | penalties missed |
| `penalties_defended` | penalties saved (goalkeepers) |
| `played_under_20m` | `true` if entered at/after the 70' **or** subbed out before the 20' |

## Requirements

- Python 3.10+
- `requests`  (`pip install -r requirements.txt`)

## Command-line usage

```bash
# Crawl a round by number (resolves fase automatically). Default = current round.
python crawler.py --jornada 31 --out liga_jornada31.json

# Just print the competition variables (fase, id_edicao, rounds, current_round)
python crawler.py --list-rounds

# Crawl a specific round URL
python crawler.py --url "https://www.zerozero.pt/competicao/liga-portuguesa?jornada_in=32&fase=217930" --out liga_jornada32.json

# Crawl a single match (prints its JSON to stdout)
python crawler.py --match "https://www.zerozero.pt/jogo/2026-04-25-benfica-moreirense/11071718"

# Tune the polite delay (seconds) between requests
python crawler.py --jornada 31 --delay 2.0
```

Without `--out` the JSON is printed to stdout; progress goes to stderr.

## How it works

1. **Competition page** → the `form_edicao` form yields `fase`, `id_edicao`
   and the `jornada_in` round list (`get_competition`).
2. **Round page** (`#fixture_games` table) → the authoritative list of games,
   each team's `name` + `id` (the id is read from the crest image filename,
   which is present even for clubs whose links omit it), and the final score
   (`get_fixture` / `parse_round_games`).
3. **Each match page** → the lineup block, segmented by the `<div class="subtitle">`
   markers (`Home`, `Away`, `Suplentes`, `Suplentes`, `Treinadores`). A player
   `<div>` flagged `inactive` is a greyed-out unused sub and is skipped
   (`get_match` / `parse_lineups`).
4. **Events** are read from each player's `events` div. A single goal icon can
   represent a brace — the minute `<div>` lists every minute (`89' 90+1'`), with
   per-minute annotations: `(g.p.)` = penalty goal, `(p.b.)` = own goal.

### Event-icon mapping (verified)

| event | signal |
|-------|--------|
| goal | `zz-icn-fut-11` / `title="Golos"` |
| penalty goal | goal icon + `(g.p.)` in the minute |
| own goal | goal icon + `(p.b.)` in the minute |
| assist | `title="Assistência"` |
| yellow | `title="Amarelos"` |
| red | `title="Vermelhos"` or an `icn_zerozero red` glyph |
| sub on | `title="Entrou"` + minute |
| sub off | untitled `icn_zerozero grey` glyph + minute |

> **Note on penalties missed / defended.** Round 31 contained no missed or
> saved penalties, so those icons could not be observed first-hand. The crawler
> detects them heuristically (event titles containing `falhad` →
> missed; `defendid`/`defes` + `penal` → defended) and **logs any unrecognised
> event type to stderr** (`? unmapped events in <game>: ...`). If you crawl a
> round that has one and see such a log line, add the exact title to
> `classify_events` in `crawler.py`.

## Cup & international competitions

The crawler handles more than club leagues:

- **National-team competitions** (e.g. the World Cup) identify teams by flag,
  not club crest, and their `/equipa/` links omit the numeric id. The team ids
  are read from each fixture row's head-to-head link as a fallback.
- **Knockout phases** (`Oitavos-de-Final`, `Quartos`, `Final`, …) are addressed
  by `fase` rather than a `jornada` number. `get_competition` discovers them
  (`comp["phases"]`); a `--backfill` run crawls the group stage and then each
  knockout phase, mapping it to a `round` number continuing after the group
  jornadas and storing the readable name in `matches.phase` (apply
  `db/migration_knockout_phase.sql` first; league rounds leave it `NULL`).
- **Not-yet-decided brackets** ("2A" vs "2B" before the groups are played) use a
  placeholder team id; it's stored as no team (the real team fills in once the
  draw resolves — the match id is stable).

```bash
python sync.py --competition mundial --backfill   # group stage + every knockout phase
```

## Competition squads (Teams & Players)

Beyond fixtures, a competition can own its **teams** and their **players**.
`roster.py` is the squad-shaped twin of `crawler.py` (it reuses the same HTTP
session, encoding handling and name cleaning); `roster_sync.py` is the twin of
`sync.py`. See `docs/COMPETITION_BACKOFFICE_DESIGN.md` for the full design.

```python
import crawler, roster
comp = roster.augment_competition(crawler.get_competition(URL))  # resolves epoca_id + season
squad = roster.get_team_roster(comp["epoca_id"], {"id": "32"})    # squad grouped by position
detail = roster.get_player_detail("547211")                       # detailed "Posição", age, club
```

What it extracts (verified against Liga Portugal, época 155):

- **Squad** from `/equipa/<id>?epoca_id=<season>` — players under the four
  `<div class="section">` groups (Guarda Redes / Defesa / Médio / Avançado),
  each with shirt number and inline age.
- **Detailed position** from `/jogador/<id>` — the `Posição` card value
  (e.g. `Médio Centro`), normalized to an export code (`CM`) via
  `roster.POSITION_CODES`. Unmapped positions log `? unmapped position: …` to
  stderr (extend the map), exactly like the event-icon vocabulary above.

Team discovery is **not** scraped from the competition page (it only lists one
round + unrelated clubs); `roster_sync.team_ids_from_matches` derives the team
set from the fixtures already in the DB, so crawl a league's rounds first.

```bash
# Fast sync: teams + rosters + position groups (no player pages). ~1+18 requests.
python roster_sync.py --competition liga-portuguesa
# Full sync: also opens each player page for detailed position/age/club.
python roster_sync.py --competition liga-portuguesa --full
# One team's roster, or one player's metadata:
python roster_sync.py --competition liga-portuguesa --team 32
python roster_sync.py --player 547211
# Standalone parser test (no DB):
python roster.py --team 32 --full
```

Apply `db/migration_competition_squads.sql` (schema) and, for export,
`db/migration_entity_export.sql` before running. The backoffice lives at
`/competitions` in the web app.
