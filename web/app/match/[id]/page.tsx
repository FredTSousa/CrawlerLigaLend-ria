import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import CrawlButton from "@/components/CrawlButton";

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
      "id, round, played_on, home_score, away_score, url, home_team_id, away_team_id, " +
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
  const all = (players ?? []) as any[];

  if (!m) {
    return (
      <div className="panel">
        <p>Match not found. It may not have been crawled yet.</p>
        <Link href="/">← Dashboard</Link>
      </div>
    );
  }

  const sortPlayers = (teamId: string) =>
    all
      .filter((p) => p.team_id === teamId)
      .sort(
        (a, b) =>
          Number(b.is_starter) - Number(a.is_starter) ||
          (a.shirt_number ?? 99) - (b.shirt_number ?? 99),
      );

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div>
            <div className="muted">
              Round {m.round} · {m.played_on ?? ""}
            </div>
            <h2 style={{ margin: "6px 0" }}>
              {m.home_team?.name}{" "}
              <span className="score">
                {m.home_score ?? "–"}-{m.away_score ?? "–"}
              </span>{" "}
              {m.away_team?.name}
            </h2>
          </div>
          <CrawlButton kind="match" target={m.url} label="Re-crawl match" />
        </div>
      </div>

      <TeamTable title={m.home_team?.name} rows={sortPlayers(m.home_team_id)} />
      <TeamTable title={m.away_team?.name} rows={sortPlayers(m.away_team_id)} />

      <p>
        <Link href={`/round/${m.round}`}>← Round {m.round}</Link>
      </p>
    </>
  );
}

function TeamTable({ title, rows }: { title: string; rows: any[] }) {
  if (!rows.length) return null;
  return (
    <div className="panel">
      <h2>{title}</h2>
      <table>
        <thead>
          <tr>
            <th className="num">#</th>
            <th>Player</th>
            <th className="num">G</th>
            <th className="num">A</th>
            <th className="num">YC</th>
            <th className="num">RC</th>
            <th className="num">OG</th>
            <th className="num">Pen ✓/✗/🧤</th>
            <th className="num">In/Out</th>
            <th className="num">&lt;20′</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.player_id}>
              <td className="num muted">{p.shirt_number ?? ""}</td>
              <td>
                {p.player_name}
                {p.is_captain && <span className="badge">C</span>}
                {!p.is_starter && <span className="badge">sub</span>}
              </td>
              <td className="num">{p.goals || ""}</td>
              <td className="num">{p.assists || ""}</td>
              <td className="num tag-yellow">{p.yellow_cards || ""}</td>
              <td className="num tag-red">{p.red_card ? "●" : ""}</td>
              <td className="num">{p.own_goals || ""}</td>
              <td className="num">
                {p.penalties_scored || 0}/{p.penalties_missed || 0}/
                {p.penalties_defended || 0}
              </td>
              <td className="num muted">
                {p.entered_min != null ? `▲${p.entered_min}'` : ""}
                {p.left_min != null ? ` ▼${p.left_min}'` : ""}
              </td>
              <td className="num">{p.played_under_20m ? "✓" : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
