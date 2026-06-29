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
    .select("player_id, player_name, age, position_group, position, club_name, photo_url, shirt_number, last_updated, active")
    .eq("competition_id", id)
    .eq("team_id", teamId);

  const rows = (roster ?? []) as any[];
  const activeRows = rows.filter((p) => p.active !== false);
  const byGroup = new Map<string, any[]>();
  for (const p of activeRows) {
    const g = p.position_group ?? "Other";
    if (!byGroup.has(g)) byGroup.set(g, []);
    byGroup.get(g)!.push(p);
  }
  const departed = rows.filter((p) => p.active === false);
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
                · {comp?.season ?? ""} · {activeRows.length} players{departed.length ? ` · ${departed.length} departed` : ""}
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

      {activeRows.length ? (
        groups.map((g) => (
          <div key={g} className="panel">
            <h3 style={{ marginTop: 0 }}>{g}</h3>
            <PlayerTable players={byGroup.get(g)!} />
          </div>
        ))
      ) : (
        <div className="panel">
          <p className="muted">No roster yet — press "Refresh roster".</p>
        </div>
      )}

      {departed.length > 0 && (
        <div className="panel">
          <h3 style={{ marginTop: 0, opacity: 0.5 }}>Departed</h3>
          <PlayerTable players={departed} dim />
        </div>
      )}
    </>
  );
}

function PlayerTable({ players, dim }: { players: any[]; dim?: boolean }) {
  return (
    <table style={dim ? { opacity: 0.45 } : undefined}>
      <thead>
        <tr>
          <th className="num">#</th>
          <th></th>
          <th>Name</th>
          <th className="num">Age</th>
          <th>Position</th>
          <th>Updated</th>
        </tr>
      </thead>
      <tbody>
        {players
          .sort((a, b) => (a.shirt_number ?? 99) - (b.shirt_number ?? 99))
          .map((p) => (
            <tr key={p.player_id}>
              <td className="num">{p.shirt_number ?? "—"}</td>
              <td><Avatar src={p.photo_url} name={p.player_name} /></td>
              <td><Link href={`/players/${p.player_id}`}>{p.player_name}</Link></td>
              <td className="num">{p.age ?? "—"}</td>
              <td>{p.position ?? <span className="muted">not enriched</span>}</td>
              <td className="muted">{fmt(p.last_updated)}</td>
            </tr>
          ))}
      </tbody>
    </table>
  );
}

function Avatar({ src, name }: { src: string | null; name: string | null }) {
  if (!src) {
    // No headshot crawled — show a clear, muted placeholder so a failed/partial
    // photo crawl is obvious at a glance.
    return (
      <div
        title="no photo"
        style={{
          width: 32,
          height: 32,
          borderRadius: "50%",
          background: "var(--border, #2a2a2a)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          fontSize: 11,
          color: "var(--muted, #888)",
        }}
      >
        ?
      </div>
    );
  }
  return (
    <img
      src={src}
      alt={name ?? ""}
      width={32}
      height={32}
      loading="lazy"
      style={{ borderRadius: "50%", objectFit: "cover", display: "block" }}
    />
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
