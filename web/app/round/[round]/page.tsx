import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import CrawlButton from "@/components/CrawlButton";

export const dynamic = "force-dynamic";

export default async function RoundPage({
  params,
}: {
  params: { round: string };
}) {
  const round = Number(params.round);
  const supabase = createClient();

  const { data: matches } = await supabase
    .from("matches")
    .select(
      "id, played_on, home_score, away_score, url, " +
        "home_team:teams!matches_home_team_id_fkey(id,name), " +
        "away_team:teams!matches_away_team_id_fkey(id,name)",
    )
    .eq("round", round)
    .order("played_on", { ascending: true });

  const games = (matches ?? []) as any[];

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>Round {round}</h2>
          <CrawlButton kind="round" target={round} label="Re-crawl round" />
        </div>
      </div>

      <div className="panel">
        {games.length ? (
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th style={{ textAlign: "right" }}>Home</th>
                <th className="num">Result</th>
                <th>Away</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {games.map((g) => (
                <tr key={g.id}>
                  <td className="muted">{g.played_on ?? "—"}</td>
                  <td style={{ textAlign: "right" }}>{g.home_team?.name}</td>
                  <td className="num score">
                    {g.home_score ?? "–"}-{g.away_score ?? "–"}
                  </td>
                  <td>{g.away_team?.name}</td>
                  <td>
                    <Link href={`/match/${g.id}`}>stats →</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">
            No matches stored for round {round}. Press “Re-crawl round” above.
          </p>
        )}
      </div>
      <p><Link href="/">← Dashboard</Link></p>
    </>
  );
}
