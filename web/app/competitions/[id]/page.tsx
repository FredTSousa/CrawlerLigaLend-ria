import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import SquadSyncButton from "@/components/SquadSyncButton";
import TmSettings from "@/components/TmSettings";
import RatingSourceSettings from "@/components/RatingSourceSettings";
import EmulationControl from "@/components/EmulationControl";

export const dynamic = "force-dynamic";

type SearchParams = {
  tab?: string;
  team?: string;
  group?: string;
  position?: string;
  q?: string;
};

const TABS = ["teams", "players", "fixtures", "history"] as const;
const SQUAD_KINDS = ["teams", "roster", "players", "comp_full", "comp_players", "comp_sold"];

export default async function CompetitionDetails({
  params,
  searchParams,
}: {
  params: { id: string };
  searchParams: SearchParams;
}) {
  const supabase = createClient();
  const id = params.id;
  const tab = (TABS as readonly string[]).includes(searchParams.tab ?? "")
    ? (searchParams.tab as string)
    : "teams";

  const { data: comp } = await supabase
    .from("competitions")
    .select(
      "id, name, full_name, slug, season, fase, epoca_id, teams_count, players_count, last_sync_at, source_url, tm_competition_code, tm_saison_id, rating_source, goal_fixtures_url, rating_mapping",
    )
    .eq("id", id)
    .maybeSingle();

  if (!comp) {
    return (
      <div className="panel">
        <h2>Competition not found</h2>
        <Link href="/competitions">← all competitions</Link>
      </div>
    );
  }

  // Teams in this competition (active membership) + per-team active player counts.
  const { data: ctRows } = await supabase
    .from("competition_teams")
    .select(
      "team_id, active, last_sync_at, source_url, team:teams(id,name,slug,logo_url,source_url)",
    )
    .eq("competition_id", id)
    .eq("active", true);

  const { data: rosterRows } = await supabase
    .from("roster_memberships")
    .select("team_id, player_id, active")
    .eq("competition_id", id)
    .eq("active", true);
  const perTeam = new Map<string, number>();
  for (const r of (rosterRows ?? []) as any[]) {
    perTeam.set(r.team_id, (perTeam.get(r.team_id) ?? 0) + 1);
  }

  const teams = ((ctRows ?? []) as any[])
    .map((r) => ({ ...r.team, playerCount: perTeam.get(r.team_id) ?? 0, membership: r }))
    .sort((a, b) => (a.name ?? "").localeCompare(b.name ?? ""));

  const { count: fixtureCount } = await supabase
    .from("matches")
    .select("id", { count: "exact", head: true })
    .eq("competition_id", id);

  // Rounds available to replay (newest first) — drives the emulation control.
  const { data: roundRows } = await supabase
    .from("matches")
    .select("round")
    .eq("competition_id", id);
  const rounds = [
    ...new Set(((roundRows ?? []) as any[]).map((r) => r.round).filter((r) => r != null)),
  ].sort((a, b) => b - a);

  const compLabel = comp.full_name || comp.name || comp.slug;

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2 style={{ marginBottom: 4 }}>{compLabel}</h2>
            <div className="muted" style={{ fontSize: 13 }}>
              comp {comp.id} · slug {comp.slug} · época {comp.epoca_id ?? "—"} ·
              fase {comp.fase ?? "—"}
            </div>
          </div>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <SquadSyncButton kind="teams" target={comp.slug || comp.id} label="Sync teams" />
            <SquadSyncButton kind="comp_players" target={comp.slug || comp.id} label="Re-fetch players" variant="secondary" />
            <SquadSyncButton kind="comp_sold" target={comp.slug || comp.id} label="Fix sold positions" variant="secondary" />
            <SquadSyncButton kind="comp_full" target={comp.slug || comp.id} label="Full sync" />
            <SquadSyncButton kind="comp_full" target={comp.slug || comp.id} force label="Full refresh" variant="secondary" />
            <a className="btn secondary" href={`/api/competitions/${id}/export`}>Export snapshot</a>
          </div>
        </div>

        {/* Summary cards */}
        <div className="grid-rounds" style={{ marginTop: 12, gap: 12 }}>
          <Card label="Teams" value={comp.teams_count ?? teams.length} />
          <Card label="Players" value={comp.players_count ?? rosterRows?.length ?? 0} />
          <Card label="Fixtures" value={fixtureCount ?? 0} />
          <Card label="Last sync" value={fmt(comp.last_sync_at)} small />
        </div>

        {/* Transfermarkt mapping — drives detailed positions on a Full sync. */}
        <div style={{ marginTop: 12 }}>
          <TmSettings
            competitionId={comp.id}
            code={comp.tm_competition_code ?? null}
            saison={comp.tm_saison_id ?? null}
            season={comp.season ?? null}
          />
        </div>

        {/* Reporter rating source (A Bola / Goal.com) + percentile mapping. */}
        <div style={{ marginTop: 12 }}>
          <RatingSourceSettings
            competitionId={comp.id}
            ratingSource={comp.rating_source ?? null}
            goalFixturesUrl={comp.goal_fixtures_url ?? null}
            ratingMapping={comp.rating_mapping ?? null}
          />
          {(comp.rating_source ?? "").toUpperCase() === "GOAL" && (
            <div style={{ marginTop: 8 }}>
              <Link href={`/competitions/${id}/goal-ratings`} className="round-chip">
                View stored Goal ratings & percentiles →
              </Link>
            </div>
          )}
        </div>

        {/* Live replay — rehearse a round on an accelerated, configurable clock. */}
        {rounds.length > 0 && (
          <div className="row" style={{ marginTop: 12, gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            <span className="muted" style={{ fontSize: 13 }}>Live replay</span>
            <EmulationControl competitionId={comp.id} rounds={rounds} />
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="panel">
        <nav className="row" style={{ gap: 6, marginBottom: 12 }}>
          {TABS.map((t) => (
            <Link
              key={t}
              href={`/competitions/${id}?tab=${t}`}
              className={`round-chip${t === tab ? " active" : ""}`}
              style={{ textTransform: "capitalize" }}
            >
              {t === "history" ? "Sync history" : t}
            </Link>
          ))}
        </nav>

        {tab === "teams" && <TeamsTab id={id} comp={comp} teams={teams} />}
        {tab === "players" && (
          <PlayersTab id={id} comp={comp} teams={teams} sp={searchParams} />
        )}
        {tab === "fixtures" && <FixturesTab id={id} />}
        {tab === "history" && <HistoryTab comp={comp} />}
      </div>
    </>
  );
}

function Card({ label, value, small }: { label: string; value: any; small?: boolean }) {
  return (
    <div className="panel" style={{ margin: 0, textAlign: "center", padding: "10px 14px" }}>
      <div style={{ fontSize: small ? 14 : 28, fontWeight: 600 }}>{value}</div>
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
    </div>
  );
}

function TeamsTab({ id, comp, teams }: { id: string; comp: any; teams: any[] }) {
  if (!teams.length) {
    return <p className="muted">No teams yet — press “Sync teams”.</p>;
  }
  return (
    <table>
      <thead>
        <tr>
          <th></th>
          <th>Name</th>
          <th>zz id</th>
          <th className="num">Players</th>
          <th>Last sync</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {teams.map((t) => (
          <tr key={t.id}>
            <td>{t.logo_url ? <img src={t.logo_url} alt="" height={20} /> : "⚽"}</td>
            <td><Link href={`/competitions/${id}/teams/${t.id}`}>{t.name}</Link></td>
            <td className="muted">{t.id}</td>
            <td className="num">{t.playerCount}</td>
            <td className="muted">{fmt(t.membership?.last_sync_at)}</td>
            <td>
              <span className="row" style={{ gap: 8 }}>
                <SquadSyncButton
                  kind="roster"
                  target={comp.slug || comp.id}
                  competition={comp.slug || comp.id}
                  team={t.id}
                  label="Refresh roster"
                  variant="secondary"
                />
                {(t.source_url || t.membership?.source_url) && (
                  <a href={t.membership?.source_url || t.source_url} target="_blank" rel="noreferrer">
                    source ↗
                  </a>
                )}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

async function PlayersTab({
  id,
  comp,
  teams,
  sp,
}: {
  id: string;
  comp: any;
  teams: any[];
  sp: SearchParams;
}) {
  const supabase = createClient();
  let q = supabase
    .from("competition_player_details")
    .select(
      "player_id, player_name, age, position_group, position, club_name, team_id, team_name, shirt_number, last_updated, source_url",
    )
    .eq("competition_id", id)
    .eq("active", true)
    .order("team_name", { ascending: true })
    .order("position_group", { ascending: true })
    .limit(1000);

  if (sp.team) q = q.eq("team_id", sp.team);
  if (sp.group) q = q.eq("position_group", sp.group);
  if (sp.position) q = q.eq("position", sp.position);
  if (sp.q) q = q.ilike("player_name", `%${sp.q}%`);

  const { data: players } = await q;
  const rows = (players ?? []) as any[];
  const groups = ["Guarda Redes", "Defesa", "Médio", "Avançado"];

  return (
    <>
      {/* Filters — plain GET form, no client JS needed */}
      <form method="get" className="row" style={{ gap: 8, marginBottom: 12, flexWrap: "wrap" }}>
        <input type="hidden" name="tab" value="players" />
        <select name="team" defaultValue={sp.team ?? ""}>
          <option value="">All teams</option>
          {teams.map((t) => (
            <option key={t.id} value={t.id}>{t.name}</option>
          ))}
        </select>
        <select name="group" defaultValue={sp.group ?? ""}>
          <option value="">All groups</option>
          {groups.map((g) => <option key={g} value={g}>{g}</option>)}
        </select>
        <input name="position" placeholder="Position" defaultValue={sp.position ?? ""} />
        <input name="q" placeholder="Name search" defaultValue={sp.q ?? ""} />
        <button className="btn" type="submit">Filter</button>
        {(sp.team || sp.group || sp.position || sp.q) && (
          <Link href={`/competitions/${id}?tab=players`} className="round-chip">clear</Link>
        )}
      </form>

      {rows.length ? (
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th className="num">Age</th>
              <th>Group</th>
              <th>Position</th>
              <th>Team</th>
              <th>Club</th>
              <th>zz id</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={`${p.team_id}-${p.player_id}`}>
                <td><Link href={`/players/${p.player_id}`}>{p.player_name}</Link></td>
                <td className="num">{p.age ?? "—"}</td>
                <td>{p.position_group ?? "—"}</td>
                <td>{p.position ?? <span className="muted">not enriched</span>}</td>
                <td>{p.team_name}</td>
                <td className="muted">{p.club_name ?? "—"}</td>
                <td className="muted">{p.player_id}</td>
                <td className="muted">{fmt(p.last_updated)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">
          No players match. Run a Full sync to populate detailed positions.
        </p>
      )}
    </>
  );
}

async function FixturesTab({ id }: { id: string }) {
  const supabase = createClient();
  const { data: matches } = await supabase
    .from("matches")
    .select("round")
    .eq("competition_id", id);
  const rounds = [...new Set(((matches ?? []) as any[]).map((m) => m.round).filter((r) => r != null))]
    .sort((a, b) => b - a);
  if (!rounds.length) return <p className="muted">No fixtures for this competition.</p>;
  return (
    <>
      <p className="muted" style={{ marginTop: 0 }}>
        Fixtures reuse the existing round view.
      </p>
      <div className="grid-rounds">
        {rounds.map((r) => (
          <Link key={r} href={`/round/${r}?comp=${id}`} className="round-chip">{r}</Link>
        ))}
      </div>
    </>
  );
}

async function HistoryTab({ comp }: { comp: any }) {
  const supabase = createClient();
  const slug = comp.slug || comp.id;
  const { data: runs } = await supabase
    .from("crawl_runs")
    .select("id, kind, trigger, status, target, games_count, error, created_at, finished_at")
    .in("kind", SQUAD_KINDS)
    .ilike("target", `${slug}%`)
    .order("created_at", { ascending: false })
    .limit(25);
  const rows = (runs ?? []) as any[];
  if (!rows.length) return <p className="muted">No squad syncs yet.</p>;
  return (
    <table>
      <thead>
        <tr>
          <th>When</th>
          <th>Kind</th>
          <th>Status</th>
          <th className="num">Rows</th>
          <th>Error</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="muted">{fmt(r.created_at)}</td>
            <td>{r.trigger} · {r.kind}</td>
            <td><span className={`pill ${r.status}`}>{r.status}</span></td>
            <td className="num">{r.games_count ?? "—"}</td>
            <td className="muted" title={r.error || ""} style={{ maxWidth: 280, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {r.error || ""}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmt(iso: string | null) {
  if (!iso) return "never";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
