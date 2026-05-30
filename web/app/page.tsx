import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import RoundPicker from "@/components/RoundPicker";
import RunsPanel from "@/components/RunsPanel";
import LiveRefresh from "@/components/LiveRefresh";
import StatusBadge from "@/components/StatusBadge";

export const dynamic = "force-dynamic";

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

  const { data: matches } = await supabase
    .from("matches")
    .select("round")
    .not("round", "is", null);

  const rounds = Array.from(
    new Set((matches ?? []).map((m: any) => m.round as number)),
  ).sort((a, b) => b - a);

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
        <h2>{comp?.name ?? "Liga Portugal"}</h2>
        <RoundPicker current={rounds[0] ?? 1} />
      </div>

      <div className="panel">
        <h2>Rounds with data</h2>
        {rounds.length ? (
          <div className="grid-rounds">
            {rounds.map((r) => (
              <Link key={r} href={`/round/${r}`} className="round-chip">
                {r}
              </Link>
            ))}
          </div>
        ) : (
          <p className="muted">
            No data yet. Pick a round above and press “Crawl round”.
          </p>
        )}
      </div>

      <div className="panel">
        <h2>Recent crawl runs</h2>
        <RunsPanel limit={12} />
      </div>
    </>
  );
}
