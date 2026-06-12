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
  last_seen_at: string | null;
};

// The kickoff cron treats a live watcher as dead after 3 min without a
// heartbeat (crawl_runs.last_seen_at), so mirror that threshold here.
const HEARTBEAT_STALE_SEC = 180;

export default function RunsPanel({
  limit = 15,
  source,
}: {
  limit?: number;
  source?: string;
}) {
  const [runs, setRuns] = useState<Run[]>([]);
  // Ticks every second so live heartbeat ages count up between data reloads.
  const [now, setNow] = useState(() => Date.now());

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

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

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
              {heartbeatBadge(r, now)}
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

function fmtAge(sec: number) {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

// Liveness indicator for the live-match watcher. Only watch runs heartbeat each
// loop (crawl_runs.last_seen_at), so the badge is meaningful only there: green
// while beating, red once it goes past the cron's 3-min "presumed dead" cutoff —
// the point at which the watcher gets restarted (or expired to error).
function heartbeatBadge(r: Run, now: number) {
  if (r.kind !== "watch") return null;
  if (r.status !== "running" && r.status !== "queued") return null;
  if (!r.last_seen_at) return null;
  const ageSec = Math.max(0, Math.round((now - new Date(r.last_seen_at).getTime()) / 1000));
  const stale = ageSec > HEARTBEAT_STALE_SEC;
  const cls = stale ? "tag-red" : "tag-green";
  const label = stale ? `no beat ${fmtAge(ageSec)}` : `♥ ${fmtAge(ageSec)}`;
  const title = stale
    ? `No heartbeat for ${fmtAge(ageSec)} — watcher presumed dead; the kickoff cron will restart it.`
    : `Live watcher heartbeat ${fmtAge(ageSec)} ago.`;
  return <span className={`${cls} badge`} title={title}>{label}</span>;
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
