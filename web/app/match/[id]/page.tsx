import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import CrawlButton from "@/components/CrawlButton";
import Lineup from "@/components/Lineup";
import LiveRefresh from "@/components/LiveRefresh";
import StatusBadge from "@/components/StatusBadge";

export const dynamic = "force-dynamic";

export default async function MatchPage({
  params,
}: {
  params: { id: string };
}) {
  const supabase = createClient();

  const { data: match } = await supabase
    .from("matches")
    .select(
      "id, round, played_on, home_score, away_score, url, status, minute, " +
        "home_team_id, away_team_id, " +
        "home_team:teams!matches_home_team_id_fkey(id,name), " +
        "away_team:teams!matches_away_team_id_fkey(id,name)",
    )
    .eq("id", params.id)
    .maybeSingle();

  const { data: players } = await supabase
    .from("match_player_details")
    .select("*")
    .eq("match_id", params.id);

  const m = match as any;

  if (!m) {
    return (
      <div className="panel">
        <p>Match not found. It may not have been crawled yet.</p>
        <Link href="/">← Dashboard</Link>
      </div>
    );
  }

  return (
    <>
      <LiveRefresh active={m.status === "live"} />
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <div className="row muted" style={{ gap: 8 }}>
              {m.round ? <span>Round {m.round} ·</span> : null}
              <span>{m.played_on ?? ""}</span>
              <StatusBadge status={m.status} minute={m.minute} />
            </div>
            <h2 style={{ margin: "6px 0" }}>
              {m.home_team?.name}{" "}
              <span className="score">
                {m.status === "scheduled"
                  ? "vs"
                  : `${m.home_score ?? "–"}-${m.away_score ?? "–"}`}
              </span>{" "}
              {m.away_team?.name}
            </h2>
          </div>
          <span className="row">
            {m.status !== "final" && (
              <CrawlButton kind="watch" target={m.url} label="Watch live" />
            )}
            <CrawlButton kind="match" target={m.url} label="Re-crawl match" />
          </span>
        </div>
      </div>

      <div className="panel">
        <Lineup
          home={{ id: m.home_team_id, name: m.home_team?.name }}
          away={{ id: m.away_team_id, name: m.away_team?.name }}
          players={(players ?? []) as any[]}
        />
      </div>

      <p>
        {m.round ? (
          <Link href={`/round/${m.round}`}>← Round {m.round}</Link>
        ) : (
          <Link href="/">← Dashboard</Link>
        )}
      </p>
    </>
  );
}
