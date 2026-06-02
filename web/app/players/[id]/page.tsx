import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import SquadSyncButton from "@/components/SquadSyncButton";

export const dynamic = "force-dynamic";

// Player detail + cross-season history (every roster_memberships row).
export default async function PlayerDetails({
  params,
}: {
  params: { id: string };
}) {
  const supabase = createClient();
  const id = params.id;

  const { data: player } = await supabase
    .from("players")
    .select(
      "id, name, slug, age, birth_date, nationality, position, position_group, position_code, club_name, source_url, enriched_at, last_sync_at, updated_at",
    )
    .eq("id", id)
    .maybeSingle();

  if (!player) {
    return (
      <div className="panel">
        <h2>Player not found</h2>
        <Link href="/competitions">← competitions</Link>
      </div>
    );
  }

  // Full membership history across seasons/teams, newest first.
  const { data: history } = await supabase
    .from("roster_memberships")
    .select(
      "competition_id, team_id, position_group, shirt_number, active, first_seen_at, left_at, " +
        "team:teams(name), competition:competitions(full_name,name,season,slug)",
    )
    .eq("player_id", id)
    .order("first_seen_at", { ascending: false });

  const rows = (history ?? []) as any[];

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2 style={{ marginBottom: 2 }}>{player.name}</h2>
            <div className="muted" style={{ fontSize: 13 }}>
              zz {player.id}
              {player.position && ` · ${player.position}`}
              {player.position_code && ` (${player.position_code})`}
            </div>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <SquadSyncButton kind="players" target={player.id} label="Refresh metadata" />
            {player.source_url && (
              <a href={player.source_url} target="_blank" rel="noreferrer">source ↗</a>
            )}
          </div>
        </div>

        <table style={{ marginTop: 12 }}>
          <tbody>
            <Row k="Age" v={player.age} />
            <Row k="Birth date" v={player.birth_date} />
            <Row k="Nationality" v={player.nationality} />
            <Row k="Position group" v={player.position_group} />
            <Row k="Position" v={player.position} />
            <Row k="Position code" v={player.position_code} />
            <Row k="Current club" v={player.club_name} />
            <Row k="Enriched" v={player.enriched_at ? fmt(player.enriched_at) : "never (run Full sync)"} />
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Roster history</h3>
        {rows.length ? (
          <table>
            <thead>
              <tr>
                <th>Season</th>
                <th>Competition</th>
                <th>Team</th>
                <th>Group</th>
                <th className="num">#</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={`${r.competition_id}-${r.team_id}-${i}`}>
                  <td>{r.competition?.season ?? "—"}</td>
                  <td>
                    <Link href={`/competitions/${r.competition_id}`}>
                      {r.competition?.full_name || r.competition?.name || r.competition_id}
                    </Link>
                  </td>
                  <td>
                    <Link href={`/competitions/${r.competition_id}/teams/${r.team_id}`}>
                      {r.team?.name ?? r.team_id}
                    </Link>
                  </td>
                  <td>{r.position_group ?? "—"}</td>
                  <td className="num">{r.shirt_number ?? "—"}</td>
                  <td>
                    {r.active
                      ? <span className="pill success">active</span>
                      : <span className="pill" title={r.left_at || ""}>left</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">No roster history recorded yet.</p>
        )}
      </div>
    </>
  );
}

function Row({ k, v }: { k: string; v: any }) {
  return (
    <tr>
      <td className="muted" style={{ width: 140 }}>{k}</td>
      <td>{v ?? "—"}</td>
    </tr>
  );
}
function fmt(iso: string) {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
