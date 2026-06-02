import Link from "next/link";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

// Competition Management — overview of every competition-season with squad counts.
export default async function CompetitionsPage() {
  const supabase = createClient();

  const { data: comps } = await supabase
    .from("competitions")
    .select(
      "id, name, full_name, slug, season, teams_count, players_count, last_sync_at",
    )
    .order("slug", { ascending: true })
    .order("season", { ascending: false });

  // Fixture counts per competition (one grouped query).
  const { data: fixtureRows } = await supabase
    .from("matches")
    .select("competition_id");
  const fixtures = new Map<string, number>();
  for (const m of (fixtureRows ?? []) as any[]) {
    if (!m.competition_id) continue;
    fixtures.set(m.competition_id, (fixtures.get(m.competition_id) ?? 0) + 1);
  }

  const rows = (comps ?? []) as any[];

  return (
    <div className="panel">
      <h2>Competitions</h2>
      {rows.length ? (
        <table>
          <thead>
            <tr>
              <th>Competition</th>
              <th>Season</th>
              <th className="num">Teams</th>
              <th className="num">Players</th>
              <th className="num">Fixtures</th>
              <th>Last sync</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => (
              <tr key={c.id}>
                <td>
                  <Link href={`/competitions/${c.id}`}>
                    {c.full_name || c.name || c.slug}
                  </Link>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {c.slug} · {c.id}
                  </div>
                </td>
                <td>{c.season ?? "—"}</td>
                <td className="num">{c.teams_count ?? "—"}</td>
                <td className="num">{c.players_count ?? "—"}</td>
                <td className="num">{fixtures.get(c.id) ?? 0}</td>
                <td className="muted">{fmt(c.last_sync_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="muted">
          No competitions yet — crawl a league from the Subscribers page, then
          sync its squads here.
        </p>
      )}
    </div>
  );
}

function fmt(iso: string | null) {
  if (!iso) return "never";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
