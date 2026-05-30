"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

type Props = {
  kind: "round" | "match";
  // round number, or match URL for kind="match"
  target: string | number;
  label?: string;
};

export default function CrawlButton({ kind, target, label }: Props) {
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
        body: JSON.stringify({ kind, target }),
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
      if (Date.now() - started < 5 * 60 * 1000) {
        setTimeout(tick, 3000);
      } else {
        setBusy(false);
      }
    };
    setTimeout(tick, 2500);
  }

  const text = label || (kind === "round" ? "Crawl round" : "Crawl match");

  return (
    <span className="row">
      <button className="btn" onClick={trigger} disabled={busy}>
        {busy ? "Crawling…" : text}
      </button>
      {status && (
        <span className={`pill ${["queued", "running", "success", "error"].includes(status) ? status : "error"}`}>
          {status}
        </span>
      )}
    </span>
  );
}
