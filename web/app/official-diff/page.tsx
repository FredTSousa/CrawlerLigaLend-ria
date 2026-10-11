import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import CrawlButton from "@/components/CrawlButton";

export const dynamic = "force-dynamic";

// zerozero vs the official Liga Portugal stats (ligaportugal.pt), which the
// league actually scores from. ligaportugal.py (job kind 'official') stores the
// official side in official_* tables; the official_stat_diff view
// (db/migration_official_stats.sql) lines both up per (match, player). Nothing
// here changes match_players: fix a real difference with ✎ on the match page.

type SearchParams = { competition?: string; stat?: string };

const STATS = [
  { key: "goals", label: "Goals" },
  { key: "assists", label: "Assists" },
  { key: "yellow_cards", label: "Yellows" },
  { key: "red_card", label: "Red card" },
  { key: "own_goals", label: "Own goals" },
  { key: "under_20m", label: "Under 20'" },
] as const;
type StatKey = (typeof STATS)[number]["key"];

type Diff = {
  match_id: string;
  player_id: string;
  round: number | null;
  stat: StatKey;
  zz: string;
  off: string;
  overridden: boolean;
  zzMissing: boolean;
};

const min = (m: number | null) => (m == null ? "" : `${m}'`);

function minutes(entered: number | null, left: number | null): string {
  const parts = [];
  if (entered != null) parts.push(`on ${min(entered)}`);
  if (left != null) parts.push(`off ${min(left)}`);
  return parts.join(", ") || "full match";
}

// One diff-view row -> one entry per stat that differs.
function explode(r: any): Diff[] {
  const out: Diff[] = [];
  const base = {
    match_id: r.match_id,
    player_id: r.player_id,
    round: r.round,
    zzMissing: r.zz_missing,
  };
  const ov = r.stat_overrides ?? {};
  const add = (stat: StatKey, zz: string, off: string, ovKey?: string) =>
    out.push({ ...base, stat, zz, off, overridden: !!ovKey && ovKey in ov });

  if (r.zz_goals !== r.off_goals) add("goals", String(r.zz_goals), String(r.off_goals), "goals");
  if (r.off_assists != null && r.zz_assists !== r.off_assists)
    add("assists", String(r.zz_assists), String(r.off_assists), "assists");
  if (r.zz_yellow_cards !== r.off_yellow_cards)
    add("yellow_cards", String(r.zz_yellow_cards), String(r.off_yellow_cards), "yellow_cards");
  if (r.zz_red_card !== r.off_red_card)
    add("red_card", r.zz_red_card ? "yes" : "no", r.off_red_card ? "yes" : "no");
  if (r.zz_own_goals !== r.off_own_goals)
    add("own_goals", String(r.zz_own_goals), String(r.off_own_goals));
  if (r.zz_under_20m !== r.off_under_20m)
    add(
      "under_20m",
      `${r.zz_under_20m ? "yes" : "no"} (${minutes(r.zz_entered_min, r.zz_left_min)})`,
      `${r.off_under_20m ? "yes" : "no"} (${minutes(r.off_entered_min, r.off_left_min)})`,
    );
  return out;
}

export default async function OfficialDiffPage({
  searchParams,
}: {
  searchParams: SearchParams;
}) {
  const supabase = createClient();

  const { data: comps, error: compErr } = await supabase
    .from("competitions")
    .select("id, name, lp_competition")
    .not("lp_competition", "is", null)
    .order("updated_at", { ascending: false });

  if (compErr) {
    return (
      <>
        <Header />
        <div className="panel">
          <p className="muted">
            This page needs <code>db/migration_official_stats.sql</code> run once in the
            Supabase SQL editor first.
          </p>
        </div>
      </>
    );
  }

  const comp =
    (comps ?? []).find((c: any) => c.id === searchParams.competition) ?? comps?.[0];
  if (!comp) {
    return (
      <>
        <Header />
        <div className="panel">
          <p className="muted">
            No competition has an official source yet (set{" "}
            <code>competitions.lp_competition</code> / <code>lp_season</code>).
          </p>
        </div>
      </>
    );
  }
  const statFilter = STATS.find((s) => s.key === searchParams.stat)?.key ?? null;

  const [{ data: diffRows }, { data: syncedMatches }, { data: unmapped }] =
    await Promise.all([
      supabase
        .from("official_stat_diff")
        .select("*")
        .eq("competition_id", comp.id)
        .eq("differs", true)
        .limit(5000),
      supabase
        .from("matches")
        .select("id, official_synced_at, official_final")
        .eq("competition_id", comp.id)
        .not("official_synced_at", "is", null),
      supabase
        .from("official_match_players")
        .select("match_id, lp_name, shirt_number, matches!inner(competition_id, round)")
        .eq("matches.competition_id", comp.id)
        .is("player_id", null),
    ]);

  const all = ((diffRows ?? []) as any[]).flatMap(explode);
  const counts = new Map<StatKey, number>();
  for (const d of all) counts.set(d.stat, (counts.get(d.stat) ?? 0) + 1);
  const diffs = (statFilter ? all.filter((d) => d.stat === statFilter) : all).sort(
    (a, b) =>
      (b.round ?? 0) - (a.round ?? 0) ||
      a.match_id.localeCompare(b.match_id) ||
      a.player_id.localeCompare(b.player_id),
  );

  const synced = (syncedMatches ?? []) as any[];
  const lastSync = synced
    .map((m) => m.official_synced_at as string)
    .sort()
    .at(-1);
  const provisional = synced.filter((m) => !m.official_final).length;

  const assists = await assistTotals(supabase, comp.id);

  // Names for the rows on screen.
  const matchIds = [
    ...new Set([...diffs.map((d) => d.match_id), ...((unmapped ?? []) as any[]).map((u) => u.match_id)]),
  ];
  const playerIds = [...new Set(diffs.map((d) => d.player_id))];
  const [{ data: matches }, { data: players }] = await Promise.all([
    matchIds.length
      ? supabase
          .from("matches")
          .select(
            "id, round, home_team:teams!matches_home_team_id_fkey(name), " +
              "away_team:teams!matches_away_team_id_fkey(name)",
          )
          .in("id", matchIds)
      : Promise.resolve({ data: [] as any[] }),
    playerIds.length
      ? supabase.from("players").select("id, name").in("id", playerIds)
      : Promise.resolve({ data: [] as any[] }),
  ]);
  const mById = new Map(((matches ?? []) as any[]).map((m) => [m.id, m]));
  const pName = new Map(((players ?? []) as any[]).map((p) => [p.id, p.name]));
  const label = (id: string) => {
    const m = mById.get(id);
    return m ? `${m.home_team?.name} – ${m.away_team?.name}` : id;
  };
  const statLabel = (k: StatKey) => STATS.find((s) => s.key === k)!.label;
  const href = (stat: string | null) =>
    `/official-diff?competition=${comp.id}${stat ? `&stat=${stat}` : ""}`;

  return (
    <>
      <Header />

      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            {(comps ?? []).length > 1 &&
              (comps ?? []).map((c: any) => (
                <Link
                  key={c.id}
                  href={`/official-diff?competition=${c.id}`}
                  className={`round-chip${c.id === comp.id ? " active" : ""}`}
                >
                  {c.name}
                </Link>
              ))}
            <span className="muted" style={{ fontSize: 13 }}>
              {comp.name}: {synced.length} match{synced.length === 1 ? "" : "es"} compared
              {provisional ? ` (${provisional} not yet marked official)` : ""}
              {lastSync ? ` · last fetched ${new Date(lastSync).toLocaleString("pt-PT")}` : ""}
            </span>
          </div>
          <CrawlButton kind="official" target={comp.id} />
        </div>
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Per-match differences</h3>
        <div className="row" style={{ marginBottom: 10 }}>
          <Link href={href(null)} className={`round-chip${statFilter ? "" : " active"}`}>
            All ({all.length})
          </Link>
          {STATS.map((s) => (
            <Link
              key={s.key}
              href={href(s.key)}
              className={`round-chip${statFilter === s.key ? " active" : ""}`}
            >
              {s.label} ({counts.get(s.key) ?? 0})
            </Link>
          ))}
        </div>
        {diffs.length ? (
          <table>
            <thead>
              <tr>
                <th>Round</th>
                <th>Match</th>
                <th>Player</th>
                <th>Stat</th>
                <th>zerozero</th>
                <th>Official</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {diffs.map((d, i) => (
                <tr key={`${d.match_id}-${d.player_id}-${d.stat}-${i}`}>
                  <td>{d.round ?? "–"}</td>
                  <td>{label(d.match_id)}</td>
                  <td>
                    <Link href={`/players/${d.player_id}`}>{pName.get(d.player_id) ?? d.player_id}</Link>
                    {d.zzMissing && (
                      <span className="muted" style={{ fontSize: 12 }}> · not in zerozero lineup</span>
                    )}
                  </td>
                  <td>{statLabel(d.stat)}</td>
                  <td>
                    {d.zz}
                    {d.overridden && (
                      <span className="tag-yellow" style={{ fontSize: 12 }} title="Set by hand with ✎">
                        {" "}✎
                      </span>
                    )}
                  </td>
                  <td>
                    <b>{d.off}</b>
                  </td>
                  <td>
                    <Link href={`/match/${d.match_id}`}>fix →</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">
            {synced.length ? "No differences. 🎉" : "Nothing fetched yet: press “Fetch official stats”."}
          </p>
        )}
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          Goals include penalties on both sides. Assists only show once the official
          per-match split for that player has been fetched. ✎ = zerozero value was set by hand.
        </p>
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Season assist totals that differ</h3>
        {assists.length ? (
          <table>
            <thead>
              <tr>
                <th>Player</th>
                <th>Team</th>
                <th className="num">zerozero</th>
                <th className="num">Official</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {assists.map((a) => (
                <tr key={a.key}>
                  <td>
                    {a.player_id ? <Link href={`/players/${a.player_id}`}>{a.name}</Link> : a.name}
                  </td>
                  <td className="muted">{a.team}</td>
                  <td className="num">{a.zz}</td>
                  <td className="num">
                    <b>{a.off}</b>
                  </td>
                  <td className="muted" style={{ fontSize: 12 }}>
                    {!a.player_id
                      ? "not mapped to a zerozero player yet"
                      : a.pending
                        ? "per-match split still loading"
                        : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No differences. 🎉</p>
        )}
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          The official total can be a match ahead: the leaderboard updates before the
          match is in zerozero or marked official.
        </p>
      </div>

      {(unmapped ?? []).length > 0 && (
        <div className="panel">
          <h3 style={{ margin: "0 0 8px" }}>Official players not mapped to zerozero</h3>
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            Their official events aren't compared above. Usually an unused substitute
            (zerozero has no lineup row) or a shirt-number mismatch.
          </p>
          <table>
            <thead>
              <tr>
                <th>Round</th>
                <th>Match</th>
                <th>Official name</th>
                <th className="num">#</th>
              </tr>
            </thead>
            <tbody>
              {((unmapped ?? []) as any[]).map((u, i) => (
                <tr key={`${u.match_id}-${i}`}>
                  <td>{u.matches?.round ?? "–"}</td>
                  <td>
                    <Link href={`/match/${u.match_id}`}>{label(u.match_id)}</Link>
                  </td>
                  <td>{u.lp_name}</td>
                  <td className="num">{u.shirt_number ?? "–"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}

function Header() {
  return (
    <div className="panel">
      <h2 style={{ margin: 0 }}>zerozero vs official</h2>
      <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
        Our zerozero stats compared with ligaportugal.pt, the source the league scores
        from. Only a report: nothing is changed automatically.
      </p>
    </div>
  );
}

// Season assist totals: official leaderboard vs the sum of zerozero's per-match
// assists, per player. Only the rows that differ.
async function assistTotals(supabase: ReturnType<typeof createClient>, compId: string) {
  const [{ data: totals }, { data: zzRows }] = await Promise.all([
    supabase
      .from("official_assist_totals")
      .select("lp_player_id, lp_name, lp_team_name, total, detail_total")
      .eq("competition_id", compId),
    supabase
      .from("match_players")
      .select("player_id, assists, players(name, lp_player_id), teams(name), matches!inner(competition_id)")
      .eq("matches.competition_id", compId)
      .gt("assists", 0)
      .limit(5000),
  ]);

  const zz = new Map<string, { name: string; team: string; lp: string | null; n: number }>();
  for (const r of (zzRows ?? []) as any[]) {
    const e = zz.get(r.player_id) ?? {
      name: r.players?.name ?? r.player_id,
      team: r.teams?.name ?? "",
      lp: r.players?.lp_player_id ?? null,
      n: 0,
    };
    e.n += r.assists;
    zz.set(r.player_id, e);
  }

  const off = ((totals ?? []) as any[]).filter((t) => t.total > 0);
  const lpIds = off.map((t) => t.lp_player_id);
  const { data: mapped } = lpIds.length
    ? await supabase.from("players").select("id, name, lp_player_id").in("lp_player_id", lpIds)
    : { data: [] as any[] };
  const byLp = new Map(((mapped ?? []) as any[]).map((p) => [p.lp_player_id, p]));

  type Row = {
    key: string;
    player_id: string | null;
    name: string;
    team: string;
    zz: number;
    off: number;
    pending: boolean;
  };
  const rows: Row[] = [];
  const seen = new Set<string>();
  for (const t of off) {
    const p = byLp.get(t.lp_player_id);
    const z = p ? zz.get(p.id) : undefined;
    if (p) seen.add(p.id);
    const zzN = z?.n ?? 0;
    if (zzN !== t.total) {
      rows.push({
        key: `lp-${t.lp_player_id}`,
        player_id: p?.id ?? null,
        name: p?.name ?? t.lp_name,
        team: z?.team || t.lp_team_name || "",
        zz: zzN,
        off: t.total,
        pending: t.detail_total !== t.total,
      });
    }
  }
  // zerozero assisters the official leaderboard doesn't have. Only certain when
  // we know the player's official id (otherwise they may just be unmapped).
  for (const [pid, z] of zz) {
    if (seen.has(pid) || !z.lp) continue;
    rows.push({ key: `zz-${pid}`, player_id: pid, name: z.name, team: z.team, zz: z.n, off: 0, pending: false });
  }
  return rows.sort((a, b) => Math.abs(b.off - b.zz) - Math.abs(a.off - a.zz) || a.name.localeCompare(b.name));
}
