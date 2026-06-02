import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import SquadSyncButton from "@/components/SquadSyncButton";

export const dynamic = "force-dynamic";

const GROUP_ORDER = ["Guarda Redes", "Defesa", "Médio", "Avançado"];

export default async function TeamDetails({
  params,
}: {
  params: { id: string; teamId: string };
}) {
  const supabase = createClient();
  const { id, teamId } = params;

  const { data: comp } = await supabase
    .from("competitions")
    .select("id, slug, full_name, name, season")
    .eq("id", id)
    .maybeSingle();

  const { data: team } = await supabase
    .from("teams")
    .select("id, name, slug, logo_url, source_url")
    .eq("id", teamId)
    .maybeSingle();

  const { data: roster } = await supabase
    .from("competition_player_details")
    .select("player_id, player_name, age, position_group, position, club_name, shirt_number, last_updated")
    .eq("competition_id", id)
    .eq("team_id", teamId)
    .eq("active", true);

  const rows = (roster ?? []) as any[];
  const byGroup = new Map<string, any[]>();
  for (const p of rows) {
    const g = p.position_group ?? "Other";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g)!.push(p);
  }
  const groups = [...byGroup.keys()].sort(
    (a, b) => idx(a) - idx(b) || a.localeCompare(b),
  );

  if (!team) {
    return (
      <div className="panel">
        <h2>Team not found</h2>
        <Link href={`/competitions/${id}`}>← back to competition</Link>
      </div>
    );
  }

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div className="row" style={{ gap: 10 }}>
            {team.logo_url && <img src={team.logo_url} alt="" height={32} />}
            <div>
              <h2 style={{ marginBottom: 2 }}>{team.name}</h2>
              <div className="muted" style={{ fontSize: 13 }}>
                <Link href={`/competitions/${id}`}>
                  {comp?.full_name || comp?.name}
                </Link>{" "}
                · {comp?.season ?? ""} · {rows.length} players
              </div>
            </div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <SquadSyncButton
              kind="roster"
              target={comp?.slug || id}
              competition={comp?.slug || id}
              team={teamId}
              label="Refresh roster"
            />
            {team.source_url && (
              <a href={team.source_url} target="_blank" rel="noreferrer">source ↗</a>
            )}
          </div>
        </div>
      </div>

      {rows.length ? (
        groups.map((g) => (
          <div key={g} className="panel">
            <h3 style={{ marginTop: 0 }}>{g}</h3>
            <table>
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>Name</th>
                  <th className="num">Age</th>
                  <th>Position</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {byGroup.get(g)!
                  .sort((a, b) => (a.shirt_number ?? 99) - (b.shirt_number ?? 99))
                  .map((p) => (
                    <tr key={p.player_id}>
                      <td className="num">{p.shirt_number ?? "—"}</td>
                      <td><Link href={`/players/${p.player_id}`}>{p.player_name}</Link></td>
                      <td className="num">{p.age ?? "—"}</td>
                      <td>{p.position ?? <span className="muted">not enriched</span>}</td>
                      <td className="muted">{fmt(p.last_updated)}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        ))
      ) : (
        <div className="panel">
          <p className="muted">No roster yet — press “Refresh roster”.</p>
        </div>
      )}
    </>
  );
}

function idx(g: string) {
  const i = GROUP_ORDER.indexOf(g);
  return i === -1 ? 99 : i;
}
function fmt(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
