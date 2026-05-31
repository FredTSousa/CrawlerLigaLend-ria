"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { createClient } from "@/lib/supabase/client";

type Run = {
  id: number;
  trigger: string | null;
  kind: string | null;
  target: string | null;
  status: string;
  games_count: number | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
};

export default function RunsPanel({
  limit = 15,
  source,
}: {
  limit?: number;
  source?: string;
}) {
  const [runs, setRuns] = useState<Run[]>([]);

  useEffect(() => {
    const supabase = createClient();
    let active = true;
    const load = async () => {
      let q = supabase
        .from("crawl_runs")
        .select("*")
        .order("created_at", { ascending: false })
        .limit(limit);
      if (source) q = q.eq("source", source);
      const { data } = await q;
      if (active && data) setRuns(data as Run[]);
    };
    load();
    const t = setInterval(load, 4000);
    return () => {
      active = false;
      clearInterval(t);
    };
  }, [limit, source]);

  if (!runs.length) return <p className="muted">No crawl runs yet.</p>;

  return (
    <table>
      <thead>
        <tr>
          <th>When</th>
          <th>Trigger</th>
          <th>Target</th>
          <th>Status</th>
          <th className="num">Games</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id}>
            <td className="muted">{fmt(r.created_at)}</td>
            <td>{r.trigger} · {r.kind}</td>
            <td title={r.target || ""}>{targetCell(r)}</td>
            <td>
              <span className={`pill ${r.status}`}>{r.status}</span>
              {r.error && <span className="tag-red badge" title={r.error}>err</span>}
            </td>
            <td className="num">{r.games_count ?? "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmt(iso: string) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function targetCell(r: Run) {
  const t = r.target;
  if (!t) return "—";
  if (r.kind === "match" || t.startsWith("http")) {
    const idM = t.match(/\/(\d+)(?:[/?#]|$)/);
    const slugM = t.match(/\/jogo\/([^/]+)\//);
    const label = slugM ? slugM[1] : t;
    return idM ? <Link href={`/match/${idM[1]}`}>{label}</Link> : label;
  }
  return `Round ${t}`;
}
