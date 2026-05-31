import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import TeamAliases from "@/components/TeamAliases";

export const dynamic = "force-dynamic";

// Manage the alternative names A Bola uses for each club (e.g. AFS -> Aves SAD).
// reporter_sync loads these so the A Bola search uses the right name.
export default async function TeamAliasesPage() {
  const supabase = createClient();

  const { data: teams } = await supabase
    .from("teams")
    .select("id,name")
    .order("name");
  const { data: aliases } = await supabase
    .from("team_aliases")
    .select("id,team_id,alias")
    .order("alias");

  const byTeam = new Map<string, any[]>();
  for (const a of (aliases ?? []) as any[]) {
    const arr = byTeam.get(a.team_id) ?? [];
    arr.push(a);
    byTeam.set(a.team_id, arr);
  }
  const rows = ((teams ?? []) as any[]).map((t) => ({
    id: t.id,
    name: t.name,
    aliases: byTeam.get(t.id) ?? [],
  }));

  return (
    <>
      <div className="panel">
        <h2 style={{ margin: 0 }}>Team aliases</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 8 }}>
          Alternative names A Bola uses for a club — e.g. <b>AFS → Aves SAD</b>.
          The reporter crawler searches A Bola with these, so notas/crónica pages
          are found even when zerozero and A Bola spell the club differently.
        </p>
      </div>

      <div className="panel">
        {rows.length ? (
          <TeamAliases teams={rows} />
        ) : (
          <p className="muted">No teams yet — crawl a round first.</p>
        )}
      </div>

      <p>
        <Link href="/">← Dashboard</Link>
      </p>
    </>
  );
}
