"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

// Chains two jobs behind one button: a fixtures backfill (crawls every open
// round AND any knockout phase not yet final — sync.py's run_backfill /
// _CRAWL_PHASE) followed by a players-only squad resync (roster_sync.py
// --players-only). One click catches a competition all the way up: new
// knockout-round matches plus any player wrongly parsed as departed.
type Props = {
  target: string;
  label?: string;
};

export default function FullBackfillButton({ target, label = "Full backfill" }: Props) {
  const router = useRouter();
  const [stage, setStage] = useState<"matches" | "players" | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const busy = stage !== null;

  async function triggerJob(kind: string) {
    const res = await fetch("/api/crawl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, target }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Failed to trigger");
    return waitFor(data.run_id);
  }

  function waitFor(runId: number): Promise<void> {
    const supabase = createClient();
    const started = Date.now();
    return new Promise((resolve, reject) => {
      const tick = async () => {
        const { data } = await supabase
          .from("crawl_runs")
          .select("status,error")
          .eq("id", runId)
          .single();
        if (data?.status === "success") {
          resolve();
          return;
        }
        if (data?.status === "error") {
          reject(new Error(data.error || "crawl failed"));
          return;
        }
        if (Date.now() - started > 30 * 60 * 1000) {
          reject(new Error("timed out"));
          return;
        }
        setTimeout(tick, 4000);
      };
      setTimeout(tick, 2500);
    });
  }

  async function run() {
    try {
      setStage("matches");
      setStatus("crawling matches…");
      await triggerJob("backfill");

      setStage("players");
      setStatus("re-fetching players…");
      await triggerJob("comp_players");

      setStatus("done");
      setStage(null);
      router.refresh();
    } catch (e: any) {
      setStatus("error: " + e.message);
      setStage(null);
    }
  }

  function pillClass() {
    if (!status) return "";
    if (status === "done") return "success";
    if (status.startsWith("error")) return "error";
    return "running";
  }

  return (
    <span className="row">
      <button className="btn" onClick={run} disabled={busy}>
        {stage === "matches"
          ? "Crawling matches…"
          : stage === "players"
            ? "Re-fetching players…"
            : label}
      </button>
      {status && <span className={`pill ${pillClass()}`}>{status}</span>}
    </span>
  );
}
