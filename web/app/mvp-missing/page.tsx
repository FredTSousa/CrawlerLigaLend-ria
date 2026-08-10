import Link from "next/link";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

// Season-wide worklist: matches whose reporter ratings were fetched (so the
// crónica/notas pages really were parsed) but which ended up with NO player
// flagged as MVP. Sometimes that's correct -- the source article genuinely
// never names a standout -- but the user wants these surfaced so they can
// check the printed edition and set the MVP by hand (★ MVP toggle on the
// match page). Driven by reporter_link_status.has_ratings/has_mvp, see
// db/migration_mvp_flag.sql.
export default async function MvpMissingPage() {
  const supabase = createClient();

  const { data: status, error } = await supabase
    .from("reporter_link_status")
    .select("*")
    .eq("fetched", true)
    .eq("has_ratings", true)
    .eq("has_mvp", false);

  // has_ratings/has_mvp don't exist until migration_mvp_flag.sql has been run.
  const migrationPending = !!error;

  const issues = ((status ?? []) as any[]).sort(
    (a, b) => (b.round ?? 0) - (a.round ?? 0) || (b.played_on ?? "").localeCompare(a.played_on ?? ""),
  );

  const ids = issues.map((s) => s.match_id);
  const { data: matches } = ids.length
    ? await supabase
        .from("matches")
        .select(
          "id, round, played_on, " +
            "home_team:teams!matches_home_team_id_fkey(name), " +
            "away_team:teams!matches_away_team_id_fkey(name)",
        )
        .in("id", ids)
    : { data: [] as any[] };
  const mById = new Map(((matches ?? []) as any[]).map((m) => [m.id, m]));

  return (
    <>
      <div className="panel">
        <h2 style={{ margin: 0 }}>MVP missing</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          Matches with reporter ratings fetched but no player flagged as MVP. Often the
          source article just never named one (nothing to fix) — check the printed
          edition and set it by hand with the ★ MVP button on the match page.
        </p>
      </div>

      {migrationPending ? (
        <div className="panel">
          <p className="muted">
            This page needs <code>db/migration_mvp_flag.sql</code> run once in the
            Supabase SQL editor first.
          </p>
        </div>
      ) : issues.length ? (
        <div className="panel">
          <table>
            <thead>
              <tr>
                <th>Round</th>
                <th>Date</th>
                <th>Match</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {issues.map((s) => {
                const m = mById.get(s.match_id);
                return (
                  <tr key={s.match_id}>
                    <td>{s.round ?? "–"}</td>
                    <td className="muted">{m?.played_on ?? s.played_on ?? "–"}</td>
                    <td>
                      {m
                        ? `${m.home_team?.name} – ${m.away_team?.name}`
                        : s.match_id}
                    </td>
                    <td>
                      <Link href={`/match/${s.match_id}`}>check →</Link>
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
            No gaps — every fetched match with ratings has an MVP set. 🎉
          </p>
        </div>
      )}

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
