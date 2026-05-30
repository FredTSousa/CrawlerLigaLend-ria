import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import RoundPicker from "@/components/RoundPicker";
import MatchPicker from "@/components/MatchPicker";
import RunsPanel from "@/components/RunsPanel";
import LiveRefresh from "@/components/LiveRefresh";
import StatusBadge from "@/components/StatusBadge";

export const dynamic = "force-dynamic";

const MISC = "Miscellaneous";

export default async function Dashboard() {
  const supabase = createClient();

  const { data: live } = await supabase
    .from("matches")
    .select(
      "id, home_score, away_score, status, minute, " +
        "home_team:teams!matches_home_team_id_fkey(name), " +
        "away_team:teams!matches_away_team_id_fkey(name)",
    )
    .eq("status", "live");

  const liveGames = (live ?? []) as any[];

  // All matches, grouped by league below.
  const { data: matches } = await supabase
    .from("matches")
    .select(
      "id, round, status, played_on, competition_id, " +
        "home_team:teams!matches_home_team_id_fkey(name), " +
        "away_team:teams!matches_away_team_id_fkey(name), " +
        "competition:competitions(id,name)",
    )
    .order("round", { ascending: false })
    .order("played_on", { ascending: false });

  type Group = {
    id: string | null;
    name: string;
    rounds: Set<number>;
    loose: any[]; // matches without a round (one-offs)
  };
  const groups = new Map<string, Group>();
  for (const m of (matches ?? []) as any[]) {
    const key = m.competition_id ?? "__misc__";
    const name = m.competition?.name ?? MISC;
    let g = groups.get(key);
    if (!g) {
      g = { id: m.competition_id ?? null, name, rounds: new Set(), loose: [] };
      groups.set(key, g);
    }
    if (m.round != null) g.rounds.add(m.round as number);
    else g.loose.push(m);
  }
  const leagues = [...groups.values()].sort(
    (a, b) =>
      (a.name === MISC ? 1 : 0) - (b.name === MISC ? 1 : 0) ||
      a.name.localeCompare(b.name),
  );

  const { data: comp } = await supabase
    .from("competitions")
    .select("name")
    .limit(1)
    .maybeSingle();

  return (
    <>
      <LiveRefresh active={liveGames.length > 0} />

      {liveGames.length > 0 && (
        <div className="panel">
          <h2>Live now</h2>
          <table>
            <tbody>
              {liveGames.map((g) => (
                <tr key={g.id}>
                  <td style={{ textAlign: "right" }}>{g.home_team?.name}</td>
                  <td className="num score">
                    {g.home_score ?? "–"}-{g.away_score ?? "–"}
                  </td>
                  <td>{g.away_team?.name}</td>
                  <td><StatusBadge status={g.status} minute={g.minute} /></td>
                  <td><Link href={`/match/${g.id}`}>stats →</Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="panel">
        <h2>Games by league</h2>
        {leagues.length ? (
          leagues.map((g) => (
            <div key={g.id ?? "misc"} style={{ marginBottom: 16 }}>
              <h3 style={{ margin: "0 0 8px" }}>{g.name}</h3>
              {g.rounds.size > 0 && (
                <div className="grid-rounds" style={{ marginBottom: 8 }}>
                  {[...g.rounds]
                    .sort((a, b) => b - a)
                    .map((r) => (
                      <Link
                        key={r}
                        href={`/round/${r}${g.id ? `?comp=${g.id}` : ""}`}
                        className="round-chip"
                      >
                        {r}
                      </Link>
                    ))}
                </div>
              )}
              {g.loose.length > 0 && (
                <table>
                  <tbody>
                    {g.loose.map((m) => (
                      <tr key={m.id}>
                        <td style={{ textAlign: "right" }}>{m.home_team?.name}</td>
                        <td className="num score">
                          {m.status === "scheduled"
                            ? "vs"
                            : `${m.home_score ?? "–"}-${m.away_score ?? "–"}`}
                        </td>
                        <td>{m.away_team?.name}</td>
                        <td className="muted">{m.played_on ?? ""}</td>
                        <td>
                          <Link href={`/match/${m.id}`}>stats →</Link>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          ))
        ) : (
          <p className="muted">No data yet — crawl a round or a match below.</p>
        )}
      </div>

      <div className="panel">
        <h2>{comp?.name ?? "Liga Portugal"} — crawl a round</h2>
        <RoundPicker current={1} />
      </div>

      <div className="panel">
        <h2>Crawl a match by URL</h2>
        <MatchPicker />
      </div>

      <div className="panel">
        <h2>Recent crawl runs</h2>
        <RunsPanel limit={12} />
      </div>
    </>
  );
}
