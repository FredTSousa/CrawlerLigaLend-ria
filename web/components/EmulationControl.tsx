"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

// Starts / stops an accelerated live replay (see db/migration_emulation.sql) for
// a chosen round. The replay length per match is configurable here (minutes ->
// match_seconds); a round spread over N calendar days takes ~N * that length.
// Hits POST /api/competitions/{id}/emulate (admin cookie auth, same as the
// squad-sync buttons).
type Props = {
  competitionId: string;
  rounds: number[];
};

export default function EmulationControl({ competitionId, rounds }: Props) {
  const router = useRouter();
  const [round, setRound] = useState<number | "">(rounds[0] ?? "");
  const [minutes, setMinutes] = useState(5); // per-match replay window
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  async function post(body: Record<string, unknown>, pending: string) {
    setBusy(true);
    setStatus(pending);
    try {
      const res = await fetch(`/api/competitions/${competitionId}/emulate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Failed");
      return data;
    } catch (e: any) {
      setStatus("error: " + e.message);
      throw e;
    } finally {
      setBusy(false);
    }
  }

  async function start() {
    if (round === "") {
      setStatus("error: pick a round");
      return;
    }
    const match_seconds = Math.max(10, Math.round(minutes * 60));
    try {
      const data = await post({ round, match_seconds }, "starting…");
      setStatus(`replaying round ${data.round} (${minutes} min/match)`);
      router.refresh();
    } catch {
      /* status already set */
    }
  }

  async function stop() {
    try {
      await post({ stop: true }, "stopping…");
      setStatus("stopped");
      router.refresh();
    } catch {
      /* status already set */
    }
  }

  if (!rounds.length) return null;

  return (
    <span className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "center" }}>
      <select
        value={round}
        onChange={(e) => setRound(e.target.value ? Number(e.target.value) : "")}
        disabled={busy}
        aria-label="Round to replay"
      >
        {rounds.map((r) => (
          <option key={r} value={r}>Round {r}</option>
        ))}
      </select>
      <label className="muted" style={{ fontSize: 13, display: "inline-flex", gap: 4, alignItems: "center" }}>
        <input
          type="number"
          min={0.5}
          step={0.5}
          value={minutes}
          onChange={(e) => setMinutes(Math.max(0.5, Number(e.target.value) || 0.5))}
          disabled={busy}
          style={{ width: 64 }}
          aria-label="Replay minutes per match"
        />
        min/match
      </label>
      <button className="btn" onClick={start} disabled={busy}>
        {busy ? "Working…" : "Start replay"}
      </button>
      <button className="btn secondary" onClick={stop} disabled={busy}>
        Stop
      </button>
      {status && (
        <span className={`pill ${status.startsWith("error") ? "error" : "running"}`}>
          {status}
        </span>
      )}
    </span>
  );
}
