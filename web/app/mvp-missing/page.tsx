import Link from "next/link";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

// Season-wide worklist for MVP problems, driven by reporter_link_status
// (db/migration_mvp_flag.sql + db/migration_mvp_score_check.sql). Two
// distinct issues, both keyed off matches whose reporter ratings were
// actually fetched:
//
//  - No MVP at all: sometimes correct (the source article genuinely never
//    names a standout) -- the user checks the printed edition and sets it
//    by hand with the ★ MVP toggle on the match page.
//  - MVP set but score missing: never correct -- A Bola always gives its
//    MVP a number, so a null score here means the scraper failed to find it
//    (a new MVP-card layout abola.py doesn't fully parse yet, e.g. Fotis
//    Ioannidis / Sporting-V. Guimarães 2026-08-14). Surfaced separately so
//    this class of bug turns up here instead of only by someone noticing a
//    star player with a blank score on a match page.
export default async function MvpMissingPage() {
  const supabase = createClient();

  const { data: status, error } = await supabase
    .from("reporter_link_status")
    .select("*")
    .eq("fetched", true)
    .eq("has_ratings", true);

  // has_ratings/has_mvp/mvp_missing_score don't exist until both migrations
  // have been run.
  const migrationPending = !!error;

  const rows = ((status ?? []) as any[]).sort(
    (a, b) => (b.round ?? 0) - (a.round ?? 0) || (b.played_on ?? "").localeCompare(a.played_on ?? ""),
  );
  const noMvp = rows.filter((s) => !s.has_mvp);
  const missingScore = rows.filter((s) => s.has_mvp && s.mvp_missing_score);

  const ids = [...new Set([...noMvp, ...missingScore].map((s) => s.match_id))];
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
        <h2 style={{ margin: 0 }}>MVP issues</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          Matches with reporter ratings fetched that have a problem with the MVP.
        </p>
      </div>

      {migrationPending ? (
        <div className="panel">
          <p className="muted">
            This page needs <code>db/migration_mvp_flag.sql</code> and{" "}
            <code>db/migration_mvp_score_check.sql</code> run once in the Supabase SQL
            editor first.
          </p>
        </div>
      ) : (
        <>
          <div className="panel">
            <h3 style={{ margin: "0 0 8px" }}>MVP has no score</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              A Bola always gives its MVP a number, so this is almost certainly a
              scraper bug (a new MVP-card layout) — not an editorial gap. Worth
              checking the crawl, not just setting it by hand.
            </p>
            <MvpTable rows={missingScore} mById={mById} />
          </div>

          <div className="panel">
            <h3 style={{ margin: "0 0 8px" }}>No MVP set</h3>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              Often the source article just never named one (nothing to fix) — check
              the printed edition and set it by hand with the ★ MVP button on the
              match page.
            </p>
            <MvpTable rows={noMvp} mById={mById} />
          </div>
        </>
      )}

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}

function MvpTable({ rows, mById }: { rows: any[]; mById: Map<string, any> }) {
  if (!rows.length) {
    return <p className="muted">No gaps here. 🎉</p>;
  }
  return (
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
        {rows.map((s) => {
          const m = mById.get(s.match_id);
          return (
            <tr key={s.match_id}>
              <td>{s.round ?? "–"}</td>
              <td className="muted">{m?.played_on ?? s.played_on ?? "–"}</td>
              <td>{m ? `${m.home_team?.name} – ${m.away_team?.name}` : s.match_id}</td>
              <td>
                <Link href={`/match/${s.match_id}`}>check →</Link>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
