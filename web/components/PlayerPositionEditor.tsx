"use client";

import { useState } from "react";

const POSITION_OPTIONS = [
  { code: "GR", label: "Guarda-Redes" },
  { code: "DC", label: "Defesa Central" },
  { code: "DL", label: "Defesa Lateral / Ala" },
  { code: "MC", label: "Médio Centro / Defensivo / Ofensivo" },
  { code: "MA", label: "Extremo / Médio Ala" },
  { code: "AV", label: "Avançado / Ponta de Lança" },
];

type Props = {
  playerId: string;
  position: string | null;
  positionCode: string | null;
  action: (id: string, position: string, code: string) => Promise<void>;
};

export default function PlayerPositionEditor({ playerId, position, positionCode, action }: Props) {
  const [editing, setEditing] = useState(false);
  const [pos, setPos] = useState(position ?? "");
  const [code, setCode] = useState(positionCode ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!editing) {
    return (
      <button className="btn secondary" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => setEditing(true)}>
        edit position
      </button>
    );
  }

  async function save() {
    if (!pos.trim() || !code) { setError("Both fields required"); return; }
    setBusy(true);
    setError(null);
    try {
      await action(playerId, pos.trim(), code);
      setEditing(false);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <label style={{ fontSize: 12, color: "var(--muted)" }}>Label</label>
        <input
          value={pos}
          onChange={(e) => setPos(e.target.value)}
          placeholder="e.g. Extremo Direito"
          style={{ fontSize: 13, padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 4, color: "inherit", width: 200 }}
        />
      </div>
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <label style={{ fontSize: 12, color: "var(--muted)" }}>Code</label>
        <select
          value={code}
          onChange={(e) => setCode(e.target.value)}
          style={{ fontSize: 13, padding: "4px 8px", background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 4, color: "inherit" }}
        >
          <option value="">— pick —</option>
          {POSITION_OPTIONS.map((o) => (
            <option key={o.code} value={o.code}>{o.code} — {o.label}</option>
          ))}
        </select>
      </div>
      {error && <span style={{ fontSize: 12, color: "var(--error, #f55)" }}>{error}</span>}
      <div className="row" style={{ gap: 8 }}>
        <button className="btn" style={{ fontSize: 12 }} onClick={save} disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button className="btn secondary" style={{ fontSize: 12 }} onClick={() => { setEditing(false); setError(null); }}>
          Cancel
        </button>
      </div>
    </div>
  );
}
