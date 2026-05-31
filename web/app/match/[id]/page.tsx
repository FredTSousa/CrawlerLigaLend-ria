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

  const { data: reporter } = await supabase
    .from("matches_reporter_link")
    .select("*")
    .eq("match_id", params.id)
    .maybeSingle();

  const m = match as any;
  const rep = reporter as any;

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
            <CrawlButton kind="reporter" target={m.id} label="Fetch reporter score" />
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

      <div className="panel">
        <h2>Reporter ratings — A Bola</h2>
        {rep ? (
          <>
            <ReporterTable title={m.home_team?.name} rows={rep.home_ratings || []} />
            <ReporterTable title={m.away_team?.name} rows={rep.away_ratings || []} />
            <p className="muted" style={{ fontSize: 13, marginTop: 10 }}>
              Source{(rep.urls || []).length > 1 ? "s" : ""}:{" "}
              {(rep.urls || []).map((u: string, i: number) => (
                <span key={u}>
                  {i > 0 ? " · " : ""}
                  <a href={u} target="_blank" rel="noreferrer">A Bola article {i + 1}</a>
                </span>
              ))}
              {rep.fetched_at && (
                <span> · fetched {new Date(rep.fetched_at).toLocaleString()}</span>
              )}
            </p>
          </>
        ) : (
          <p className="muted">
            No reporter ratings yet — press “Fetch reporter score”.
          </p>
        )}
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

function ReporterTable({ title, rows }: { title: string; rows: any[] }) {
  if (!rows?.length) return null;
  return (
    <div style={{ marginBottom: 14 }}>
      <h3 style={{ margin: "0 0 6px" }}>{title}</h3>
      <table>
        <thead>
          <tr>
            <th>Player</th>
            <th className="num">Score</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={r.player_id || r.player_name || i}>
              <td>{r.player_name}</td>
              <td className="num score">{r.score ?? "–"}</td>
              <td>{r.is_mvp ? <span className="live-badge">MVP</span> : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
