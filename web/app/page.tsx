import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import RoundPicker from "@/components/RoundPicker";
import RunsPanel from "@/components/RunsPanel";

export const dynamic = "force-dynamic";

export default async function Dashboard() {
  const supabase = createClient();

  const { data: matches } = await supabase
    .from("matches")
    .select("round")
    .not("round", "is", null);

  const rounds = Array.from(
    new Set((matches ?? []).map((m: any) => m.round as number)),
  ).sort((a, b) => b - a);

  const { data: comp } = await supabase
    .from("competitions")
    .select("name")
    .limit(1)
    .maybeSingle();

  return (
    <>
      <div className="panel">
        <h2>{comp?.name ?? "Liga Portugal"}</h2>
        <RoundPicker current={rounds[0] ?? 1} />
      </div>

      <div className="panel">
        <h2>Rounds with data</h2>
        {rounds.length ? (
          <div className="grid-rounds">
            {rounds.map((r) => (
              <Link key={r} href={`/round/${r}`} className="round-chip">
                {r}
              </Link>
            ))}
          </div>
        ) : (
          <p className="muted">
            No data yet. Pick a round above and press “Crawl round”.
          </p>
        )}
      </div>

      <div className="panel">
        <h2>Recent crawl runs</h2>
        <RunsPanel limit={12} />
      </div>
    </>
  );
}
