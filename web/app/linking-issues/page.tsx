import Link from "next/link";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

// Season-wide worklist: matches whose reporter ratings were fetched but still
// have zerozero players with no linked rating. One to-do queue for fixing
// nicknames / parsing gaps.
export default async function LinkingIssuesPage() {
  const supabase = createClient();

  const { data: status } = await supabase
    .from("reporter_link_status")
    .select("*")
    .eq("fetched", true);

  const issues = ((status ?? []) as any[])
    .filter((s) => s.linked < s.players)
    .sort((a, b) => (b.round ?? 0) - (a.round ?? 0));

  const ids = issues.map((s) => s.match_id);
  const { data: matches } = ids.length
    ? await supabase
        .from("matches")
        .select(
          "id, round, " +
            "home_team:teams!matches_home_team_id_fkey(name), " +
            "away_team:teams!matches_away_team_id_fkey(name)",
        )
        .in("id", ids)
    : { data: [] as any[] };
  const mById = new Map(((matches ?? []) as any[]).map((m) => [m.id, m]));

  return (
    <>
      <div className="panel">
        <h2 style={{ margin: 0 }}>Linking issues</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          Matches with reporter ratings fetched but unlinked players. Open one to
          link nicknames or fill parsing gaps.
        </p>
      </div>

      {issues.length ? (
        <div className="panel">
          <table>
            <thead>
              <tr>
                <th>Round</th>
                <th>Match</th>
                <th className="num">Unlinked</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {issues.map((s) => {
                const m = mById.get(s.match_id);
                return (
                  <tr key={s.match_id}>
                    <td>{s.round ?? "–"}</td>
                    <td>
                      {m
                        ? `${m.home_team?.name} – ${m.away_team?.name}`
                        : s.match_id}
                    </td>
                    <td className="num">
                      <span className="cov warn">{s.players - s.linked}</span>
                    </td>
                    <td>
                      <Link href={`/match/${s.match_id}`}>fix →</Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="panel">
          <p className="muted">
            No linking issues — every fetched match has all players linked. 🎉
          </p>
        </div>
      )}

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
