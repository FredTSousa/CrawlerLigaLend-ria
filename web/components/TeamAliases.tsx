"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";

type Alias = { id: number; alias: string };
type Team = { id: string; name: string; aliases: Alias[] };

export default function TeamAliases({ teams }: { teams: Team[] }) {
  const router = useRouter();
  const supabase = createClient();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [inputs, setInputs] = useState<Record<string, string>>({});

  async function add(teamId: string) {
    const alias = (inputs[teamId] || "").trim();
    if (!alias) return;
    setBusy(true);
    setErr(null);
    const { error } = await supabase
      .from("team_aliases")
      .insert({ team_id: teamId, alias });
    setBusy(false);
    if (error) {
      setErr(error.message);
      return;
    }
    setInputs((p) => ({ ...p, [teamId]: "" }));
    router.refresh();
  }

  async function remove(id: number) {
    setBusy(true);
    setErr(null);
    const { error } = await supabase.from("team_aliases").delete().eq("id", id);
    setBusy(false);
    if (error) {
      setErr(error.message);
      return;
    }
    router.refresh();
  }

  return (
    <>
      {err && (
        <p style={{ color: "var(--red)", marginTop: 0 }}>{err}</p>
      )}
      <table>
        <thead>
          <tr>
            <th>Team</th>
            <th>A Bola aliases</th>
            <th>Add alias</th>
          </tr>
        </thead>
        <tbody>
          {teams.map((t) => (
            <tr key={t.id}>
              <td>{t.name}</td>
              <td>
                {t.aliases.length === 0 ? (
                  <span className="muted">—</span>
                ) : (
                  t.aliases.map((a) => (
                    <span className="aliaschip" key={a.id}>
                      {a.alias}
                      <button
                        disabled={busy}
                        onClick={() => remove(a.id)}
                        title="Remove alias"
                      >
                        ×
                      </button>
                    </span>
                  ))
                )}
              </td>
              <td>
                <span className="row" style={{ gap: 6 }}>
                  <input
                    placeholder="e.g. Aves SAD"
                    value={inputs[t.id] || ""}
                    disabled={busy}
                    onChange={(e) =>
                      setInputs((p) => ({ ...p, [t.id]: e.target.value }))
                    }
                    onKeyDown={(e) => e.key === "Enter" && add(t.id)}
                  />
                  <button
                    className="btn secondary"
                    disabled={busy}
                    onClick={() => add(t.id)}
                  >
                    add
                  </button>
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
