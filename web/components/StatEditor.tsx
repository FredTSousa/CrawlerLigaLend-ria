"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

export type EditableStat = "goals" | "assists" | "yellow_cards";

const STATS: { key: EditableStat; label: string; max: number }[] = [
  { key: "goals", label: "⚽ Goals", max: 9 },
  { key: "assists", label: "👟 Assists", max: 9 },
  { key: "yellow_cards", label: "🟨 Yellows", max: 2 },
];

// Hand-correct a player's crawled goals / assists / yellows when zerozero has
// them wrong. Overrides are stored server-side and re-applied by a trigger on
// every later crawl write -- see db/migration_stat_overrides.sql.
export default function StatEditor({
  matchId,
  playerId,
  values,
  overrides,
  crawled,
  official,
}: {
  matchId: string;
  playerId: string;
  values: Record<EditableStat, number>;
  overrides: Partial<Record<EditableStat, number>>;
  crawled: Record<string, number | boolean | null>;
  official: Record<string, number | boolean>;
}) {
  const router = useRouter();
  const supabase = createClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const edited = Object.keys(overrides).length > 0;

  async function save(stat: EditableStat, value: number | null) {
    setBusy(true);
    setErr(null);
    const { error } = await supabase.rpc("reporter_set_stat", {
      p_match_id: matchId,
      p_player_id: playerId,
      p_stat: stat,
      p_value: value,
    });
    setBusy(false);
    if (error) {
      setErr(error.message);
      return;
    }
    router.refresh();
  }

  return (
    <span className="stat-edit">
      <button
        className={`stat-edit-btn${edited ? " on" : ""}`}
        title={edited ? "Stats edited by hand — click to change" : "Correct goals / assists / yellows"}
        onClick={() => setOpen((o) => !o)}
      >
        ✎
      </button>
      {open && (
        <span className="stat-edit-pop">
          {STATS.map(({ key, label, max }) => {
            const isOver = key in overrides;
            return (
              <span className="row" key={key} style={{ gap: 6 }}>
                <span style={{ width: 80 }}>{label}</span>
                <select
                  value={values[key]}
                  disabled={busy}
                  onChange={(e) => save(key, Number(e.target.value))}
                >
                  {Array.from({ length: Math.max(max, values[key]) + 1 }, (_, i) => (
                    <option key={i} value={i}>
                      {i}
                    </option>
                  ))}
                </select>
                {isOver && (
                  <button
                    className="link"
                    disabled={busy}
                    title={
                      key in official
                        ? "Drop the manual value and go back to the official one"
                        : "Drop the manual value and go back to the crawled one"
                    }
                    onClick={() => save(key, null)}
                  >
                    {key in official
                      ? "reset (official)"
                      : `reset${crawled[key] != null ? ` (zz: ${crawled[key]})` : ""}`}
                  </button>
                )}
              </span>
            );
          })}
          {err && <span style={{ color: "var(--red)", fontSize: 12 }}>{err}</span>}
        </span>
      )}
    </span>
  );
}
