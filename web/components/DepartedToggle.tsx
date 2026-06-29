"use client";

import { useState } from "react";

type Props = {
  competitionId: string;
  teamId: string;
  playerId: string;
  departed: boolean;
  action: (compId: string, teamId: string, playerId: string, departed: boolean) => Promise<void>;
};

export default function DepartedToggle({ competitionId, teamId, playerId, departed, action }: Props) {
  const [busy, setBusy] = useState(false);

  async function toggle() {
    setBusy(true);
    try {
      await action(competitionId, teamId, playerId, !departed);
    } finally {
      setBusy(false);
    }
  }

  return (
    <button
      className="btn secondary"
      style={{ fontSize: 11, padding: "2px 8px", opacity: busy ? 0.5 : 1 }}
      onClick={toggle}
      disabled={busy}
      title={departed ? "Mark as active" : "Mark as sold/departed"}
    >
      {departed ? "restore" : "mark sold"}
    </button>
  );
}
