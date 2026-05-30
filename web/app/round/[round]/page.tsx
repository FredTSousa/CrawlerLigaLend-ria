import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import CrawlButton from "@/components/CrawlButton";
import Lineup from "@/components/Lineup";
import LiveRefresh from "@/components/LiveRefresh";
import StatusBadge from "@/components/StatusBadge";

export const dynamic = "force-dynamic";

export default async function RoundPage({
  params,
  searchParams,
}: {
  params: { round: string };
  searchParams: { comp?: string };
}) {
  const round = Number(params.round);
  const comp = searchParams?.comp;
  const supabase = createClient();

  let query = supabase
    .from("matches")
    .select(
      "id, played_on, home_score, away_score, url, status, minute, home_team_id, away_team_id, " +
        "home_team:teams!matches_home_team_id_fkey(id,name), " +
        "away_team:teams!matches_away_team_id_fkey(id,name)",
    )
    .eq("round", round);
  if (comp) query = query.eq("competition_id", comp);
  const { data: matches } = await query.order("played_on", { ascending: true });

  const { data: allPlayers } = await supabase
    .from("match_player_details")
    .select("*")
    .eq("round", round);

  const games = (matches ?? []) as any[];
  const byMatch = new Map<string, any[]>();
  for (const p of (allPlayers ?? []) as any[]) {
    const arr = byMatch.get(p.match_id) ?? [];
    arr.push(p);
    byMatch.set(p.match_id, arr);
  }

  const anyLive = games.some((g) => g.status === "live");

  return (
    <>
      <LiveRefresh active={anyLive} />
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>Round {round}</h2>
          <CrawlButton kind="round" target={round} label="Re-crawl round" />
        </div>
      </div>

      {games.length ? (
        games.map((g) => (
          <details className="game" key={g.id} open={g.status === "live"}>
            <summary>
              <div className="teams">
                <span className="h">{g.home_team?.name}</span>
                <span className="score">
                  {g.status === "scheduled"
                    ? "vs"
                    : `${g.home_score ?? "–"}-${g.away_score ?? "–"}`}
                  {g.status && g.status !== "final" && (
                    <>
                      {" "}
                      <StatusBadge status={g.status} minute={g.minute} />
                    </>
                  )}
                </span>
                <span className="a">{g.away_team?.name}</span>
              </div>
            </summary>
            <div className="game-body">
              <div className="game-actions">
                <Link href={`/match/${g.id}`}>open match page →</Link>
              </div>
              <Lineup
                home={{ id: g.home_team_id, name: g.home_team?.name }}
                away={{ id: g.away_team_id, name: g.away_team?.name }}
                players={byMatch.get(g.id) ?? []}
              />
            </div>
          </details>
        ))
      ) : (
        <div className="panel">
          <p className="muted">
            No matches stored for round {round}. Press “Re-crawl round” above.
          </p>
        </div>
      )}

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
