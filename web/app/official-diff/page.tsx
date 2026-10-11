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
    .select("*")
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
  // The league only counts from this round on (competitions.official_from_round).
  const fromRound: number = comp.official_from_round ?? 1;
  const statFilter = STATS.find((s) => s.key === searchParams.stat)?.key ?? null;

  const [{ data: diffRows }, { data: syncedMatches }, { data: unmapped }] =
    await Promise.all([
      supabase
        .from("official_stat_diff")
        .select("*")
        .eq("competition_id", comp.id)
        .eq("differs", true)
        .gte("round", fromRound)
        .limit(5000),
      supabase
        .from("matches")
        .select("id, official_synced_at, official_final")
        .eq("competition_id", comp.id)
        .gte("round", fromRound)
        .not("official_synced_at", "is", null),
      supabase
        .from("official_match_players")
        .select("match_id, lp_name, shirt_number, matches!inner(competition_id, round)")
        .eq("matches.competition_id", comp.id)
        .gte("matches.round", fromRound)
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

  // Official values that changed after the first fetch of a match
  // (db/migration_official_history.sql). Empty until that migration has run.
  const { data: historyRows } = await supabase
    .from("official_stat_history")
    .select(
      "match_id, player_id, lp_name, stat, old_value, new_value, official_final, observed_at, " +
        "matches!inner(competition_id, round, kickoff_at)",
    )
    .eq("matches.competition_id", comp.id)
    .gte("matches.round", fromRound)
    .order("observed_at", { ascending: false })
    .limit(500);
  const history = (historyRows ?? []) as any[];

  // Names for the rows on screen.
  const matchIds = [
    ...new Set([
      ...diffs.map((d) => d.match_id),
      ...((unmapped ?? []) as any[]).map((u) => u.match_id),
      ...history.map((h) => h.match_id),
    ]),
  ];
  const playerIds = [
    ...new Set([...diffs.map((d) => d.player_id), ...history.map((h) => h.player_id).filter(Boolean)]),
  ];
  const [{ data: matches }, { data: players }] = await Promise.all([
    matchIds.length
      ? supabase
          .from("matches")
          .select(
            "id, round, played_on, url, lp_round, lp_fixture, " +
              "home_team:teams!matches_home_team_id_fkey(name), " +
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
  // Where to check each match by hand: zerozero's page and the official one.
  const officialUrl = (m: any) =>
    m?.lp_round != null
      ? `https://www.ligaportugal.pt/match/${comp.lp_season}/${comp.lp_competition}/` +
        `${m.lp_round}/${m.lp_fixture}?tab=match_formations`
      : null;
  const byMatch = new Map<string, Diff[]>();
  for (const d of diffs) byMatch.set(d.match_id, [...(byMatch.get(d.match_id) ?? []), d]);
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
              {comp.name}
              {fromRound > 1 ? ` from round ${fromRound}` : ""}: {synced.length} match
              {synced.length === 1 ? "" : "es"} compared
              {provisional ? ` (${provisional} not yet marked official)` : ""}
              {lastSync ? ` · last fetched ${new Date(lastSync).toLocaleString("pt-PT")}` : ""}
            </span>
          </div>
          <CrawlButton kind="official" target={comp.id} />
        </div>
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>
          Matches that differ ({byMatch.size})
        </h3>
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
        {byMatch.size ? (
          [...byMatch].map(([matchId, rows]) => {
            const m = mById.get(matchId);
            const off = officialUrl(m);
            return (
              <div key={matchId} style={{ marginBottom: 18 }}>
                <div className="row" style={{ marginBottom: 4 }}>
                  <b>
                    R{m?.round ?? "?"} · {label(matchId)}
                  </b>
                  {m?.played_on && <span className="muted">{m.played_on}</span>}
                  <span className="muted" style={{ fontSize: 13 }}>
                    {m?.url && (
                      <a href={m.url} target="_blank" rel="noreferrer">
                        zerozero ↗
                      </a>
                    )}
                    {off && (
                      <>
                        {" · "}
                        <a href={off} target="_blank" rel="noreferrer">
                          official ↗
                        </a>
                      </>
                    )}
                    {" · "}
                    <Link href={`/match/${matchId}`}>fix on match page →</Link>
                  </span>
                </div>
                <table>
                  <thead>
                    <tr>
                      <th>Player</th>
                      <th>Stat</th>
                      <th>zerozero</th>
                      <th>Official</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((d, i) => (
                      <tr key={`${d.player_id}-${d.stat}-${i}`}>
                        <td>
                          <Link href={`/players/${d.player_id}`}>
                            {pName.get(d.player_id) ?? d.player_id}
                          </Link>
                          {d.zzMissing && (
                            <span className="muted" style={{ fontSize: 12 }}>
                              {" "}· not in zerozero lineup
                            </span>
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
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          })
        ) : (
          <p className="muted">
            {synced.length ? "No differences. 🎉" : "Nothing fetched yet: press “Fetch official stats”."}
          </p>
        )}
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          Goals include penalties on both sides. Assists aren't compared: they
          stay on zerozero. ✎ = zerozero value was set by hand.
        </p>
      </div>

      <div className="panel">
        <h3 style={{ margin: "0 0 8px" }}>Official values that changed after the first fetch</h3>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Each match is fetched soon after it ends, re-checked hourly until the site marks it
          official, and once more a day after kickoff. Anything listed here was not final at
          the first fetch.
          {synced.length
            ? ` ${new Set(history.map((h) => h.match_id)).size} of ${synced.length} compared ` +
              `match${synced.length === 1 ? "" : "es"} changed.`
            : ""}
        </p>
        {history.length ? (
          <table>
            <thead>
              <tr>
                <th>Match</th>
                <th>Player</th>
                <th>Stat</th>
                <th>Before → after</th>
                <th>Seen</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h, i) => {
                const kick = h.matches?.kickoff_at ? new Date(h.matches.kickoff_at).getTime() : null;
                const hours =
                  kick != null ? (new Date(h.observed_at).getTime() - kick) / 3_600_000 : null;
                return (
                  <tr key={`${h.match_id}-${i}`}>
                    <td>
                      R{h.matches?.round} · {label(h.match_id)}
                    </td>
                    <td>{(h.player_id && pName.get(h.player_id)) || h.lp_name}</td>
                    <td>{h.stat}</td>
                    <td>
                      {h.old_value ?? "–"} → <b>{h.new_value ?? "–"}</b>
                    </td>
                    <td className="muted">
                      {hours != null ? `${hours.toFixed(1)} h after kickoff` : h.observed_at}
                      {h.official_final === false ? " · not yet official" : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : (
          <p className="muted">No changes recorded yet.</p>
        )}
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
