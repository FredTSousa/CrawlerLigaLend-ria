"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

// Backoffice editor for a competition's reporter-rating source. ABOLA keeps the
// existing A Bola scraper. GOAL discovers the match on Goal.com from the
// configured fixtures URL and converts each raw Goal rating to a 0-10 score via
// the editable raw-score-range -> score mapping below (configured per season).
// reporter_sync reads these when fetching scores. Keep in sync with rating_map.py.
const DEFAULT_MAPPING: Record<string, number> = {
  "0-5": 4,
  "5-6": 5,
  "6-6.5": 6,
  "6.5-7": 7,
  "7-7.5": 8,
  "7.5-8": 9,
  "8-10": 10,
};

type Row = { range: string; score: string };

function toRows(mapping: Record<string, number> | null): Row[] {
  const m = mapping && Object.keys(mapping).length ? mapping : DEFAULT_MAPPING;
  return Object.entries(m).map(([range, score]) => ({ range, score: String(score) }));
}

export default function RatingSourceSettings({
  competitionId,
  ratingSource,
  goalFixturesUrl,
  ratingMapping,
}: {
  competitionId: string;
  ratingSource: string | null;
  goalFixturesUrl: string | null;
  ratingMapping: Record<string, number> | null;
}) {
  const router = useRouter();
  const supabase = createClient();
  const [source, setSource] = useState((ratingSource ?? "ABOLA").toUpperCase());
  const [fixtures, setFixtures] = useState(goalFixturesUrl ?? "");
  const [rows, setRows] = useState<Row[]>(toRows(ratingMapping));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const isGoal = source === "GOAL";

  function dirty() {
    setSaved(false);
  }
  function setRow(i: number, patch: Partial<Row>) {
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
    dirty();
  }
  function addRow() {
    setRows((rs) => [...rs, { range: "", score: "" }]);
    dirty();
  }
  function removeRow(i: number) {
    setRows((rs) => rs.filter((_, j) => j !== i));
    dirty();
  }

  async function save() {
    setBusy(true);
    setErr(null);
    setSaved(false);

    if (isGoal && !fixtures.trim()) {
      setBusy(false);
      setErr("Goal fixtures URL is required when the source is GOAL.");
      return;
    }

    // Build the mapping JSON from the rows (skip blanks; validate ranges/scores).
    const mapping: Record<string, number> = {};
    for (const r of rows) {
      const range = r.range.trim();
      if (!range && r.score.trim() === "") continue;
      if (!/^\d+(\.\d+)?-\d+(\.\d+)?$/.test(range)) {
        setBusy(false);
        setErr(`Invalid range "${range}". Use "lo-hi", e.g. 70-85.`);
        return;
      }
      const score = Number(r.score);
      if (!Number.isFinite(score)) {
        setBusy(false);
        setErr(`Invalid score for range "${range}".`);
        return;
      }
      mapping[range] = score;
    }

    const { error } = await supabase
      .from("competitions")
      .update({
        rating_source: isGoal ? "GOAL" : "ABOLA",
        goal_fixtures_url: isGoal ? fixtures.trim() || null : (fixtures.trim() || null),
        rating_mapping: Object.keys(mapping).length ? mapping : null,
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
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span className="muted" style={{ fontSize: 13 }}>Rating source</span>
        <select
          value={source}
          disabled={busy}
          onChange={(e) => {
            setSource(e.target.value);
            dirty();
          }}
        >
          <option value="ABOLA">A Bola</option>
          <option value="GOAL">Goal.com</option>
        </select>

        {isGoal && (
          <>
            <span className="muted" style={{ fontSize: 13 }}>Goal fixtures URL</span>
            <input
              placeholder="https://www.goal.com/en/…/fixtures-results/…"
              value={fixtures}
              disabled={busy}
              style={{ width: 360, maxWidth: "100%" }}
              onChange={(e) => {
                setFixtures(e.target.value);
                dirty();
              }}
            />
          </>
        )}
      </div>

      {isGoal && (
        <div>
          <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
            Raw Goal rating → reporter score. Each row maps a raw-score range
            (e.g. 6.5–7) to a 0–10 score:
          </div>
          <table style={{ width: "auto" }}>
            <thead>
              <tr>
                <th>Raw score range</th>
                <th className="num">Score</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={i}>
                  <td>
                    <input
                      value={r.range}
                      placeholder="6.5-7"
                      disabled={busy}
                      style={{ width: 100 }}
                      onChange={(e) => setRow(i, { range: e.target.value })}
                    />
                  </td>
                  <td className="num">
                    <input
                      value={r.score}
                      placeholder="8"
                      disabled={busy}
                      style={{ width: 60 }}
                      onChange={(e) => setRow(i, { score: e.target.value })}
                    />
                  </td>
                  <td>
                    <button
                      className="round-chip"
                      disabled={busy}
                      onClick={() => removeRow(i)}
                      title="Remove range"
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="round-chip" disabled={busy} onClick={addRow} style={{ marginTop: 6 }}>
            + add range
          </button>
        </div>
      )}

      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <button className="btn secondary" disabled={busy} onClick={save}>
          {busy ? "saving…" : "save"}
        </button>
        {saved && <span className="muted" style={{ fontSize: 13 }}>✓ saved</span>}
        {err && <span style={{ color: "var(--red)", fontSize: 13 }}>{err}</span>}
      </div>
    </div>
  );
}
