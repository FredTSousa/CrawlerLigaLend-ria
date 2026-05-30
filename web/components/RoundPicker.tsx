"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import CrawlButton from "./CrawlButton";

export default function RoundPicker({ current }: { current?: number | null }) {
  const router = useRouter();
  const [round, setRound] = useState<number>(current ?? 1);

  return (
    <div className="row">
      <label className="muted">Round</label>
      <input
        type="number"
        min={1}
        max={50}
        value={round}
        onChange={(e) => setRound(Number(e.target.value))}
        style={{ width: 80 }}
      />
      <button className="btn secondary" onClick={() => router.push(`/round/${round}`)}>
        View
      </button>
      <CrawlButton kind="round" target={round} key={round} />
    </div>
  );
}
