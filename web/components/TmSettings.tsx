"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

// Backoffice editor for a competition's Transfermarkt mapping. The crawler reads
// these on a Full sync to fetch detailed positions from Transfermarkt instead of
// opening a zerozero page per player. The season id defaults to the season's
// start year (e.g. 2025/26 -> 2025); the override is only for odd cases.
export default function TmSettings({
  competitionId,
  code,
  saison,
  season,
}: {
  competitionId: string;
  code: string | null;
  saison: string | null;
  season: string | null;
}) {
  const router = useRouter();
  const supabase = createClient();
  const [codeInput, setCodeInput] = useState(code ?? "");
  const [saisonInput, setSaisonInput] = useState(saison ?? "");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const derived = (season ?? "").match(/(20\d{2})/)?.[1] ?? null;

  async function save() {
    setBusy(true);
    setErr(null);
    setSaved(false);
    const { error } = await supabase
      .from("competitions")
      .update({
        tm_competition_code: codeInput.trim() || null,
        tm_saison_id: saisonInput.trim() || null,
        updated_at: new Date().toISOString(),
      })
      .eq("id", competitionId);
    setBusy(false);
    if (error) {
      setErr(error.message);
      return;
    }
    setSaved(true);
    router.refresh();
  }

  return (
    <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
      <span className="muted" style={{ fontSize: 13 }}>Transfermarkt code</span>
      <input
        placeholder="e.g. PO1"
        value={codeInput}
        disabled={busy}
        style={{ width: 90 }}
        onChange={(e) => {
          setCodeInput(e.target.value);
          setSaved(false);
        }}
        onKeyDown={(e) => e.key === "Enter" && save()}
      />
      <span className="muted" style={{ fontSize: 13 }}>season id</span>
      <input
        placeholder={derived ? `auto: ${derived}` : "saison_id"}
        value={saisonInput}
        disabled={busy}
        style={{ width: 110 }}
        onChange={(e) => {
          setSaisonInput(e.target.value);
          setSaved(false);
        }}
        onKeyDown={(e) => e.key === "Enter" && save()}
      />
      <button className="btn secondary" disabled={busy} onClick={save}>
        {busy ? "saving…" : "save"}
      </button>
      {saved && <span className="muted" style={{ fontSize: 13 }}>✓ saved</span>}
      {err && <span style={{ color: "var(--red)", fontSize: 13 }}>{err}</span>}
    </div>
  );
}
