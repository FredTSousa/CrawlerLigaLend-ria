"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

// Triggers a squad sync (squads.yml -> roster_sync.py) via POST /api/crawl and
// polls the crawl_runs row to completion, mirroring CrawlButton. Used on the
// Competition details and Team/Player pages.
type Props = {
  kind: "teams" | "roster" | "players" | "comp_full";
  target: string; // competition slug/URL, or player id (kind="players")
  competition?: string; // explicit competition for kind="roster"
  team?: string; // team id for kind="roster"
  full?: boolean;
  force?: boolean;
  label: string;
  variant?: "primary" | "secondary";
};

export default function SquadSyncButton(props: Props) {
  const { kind, target, competition, team, force, label, variant = "primary" } =
    props;
  const router = useRouter();
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function trigger() {
    setBusy(true);
    setStatus("queued");
    try {
      const res = await fetch("/api/crawl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, target, competition, team, force }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed to trigger");
      await poll(data.run_id);
    } catch (e: any) {
      setStatus("error: " + e.message);
      setBusy(false);
    }
  }

  async function poll(runId: number) {
    const supabase = createClient();
    const started = Date.now();
    const tick = async () => {
      const { data } = await supabase
        .from("crawl_runs")
        .select("status,error")
        .eq("id", runId)
        .single();
      if (data) {
        setStatus(data.status);
        if (data.status === "success") {
          setBusy(false);
          router.refresh();
          return;
        }
        if (data.status === "error") {
          setStatus("error: " + (data.error || "see crawl runs"));
          setBusy(false);
          return;
        }
      }
      // A full sync opens every player page, so allow a long ceiling.
      if (Date.now() - started < 30 * 60 * 1000) setTimeout(tick, 4000);
      else setBusy(false);
    };
    setTimeout(tick, 2500);
  }

  function pillClass(s: string) {
    if (s === "running" || s === "queued") return "running";
    if (s === "success") return "success";
    return "error";
  }

  return (
    <span className="row">
      <button
        className={`btn${variant === "secondary" ? " secondary" : ""}`}
        onClick={trigger}
        disabled={busy}
      >
        {busy ? "Syncing…" : label}
      </button>
      {status && <span className={`pill ${pillClass(status)}`}>{status}</span>}
    </span>
  );
}
