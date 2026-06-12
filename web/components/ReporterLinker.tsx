"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { createClient } from "@/lib/supabase/client";
import { norm } from "@/lib/norm";

type Rating = {
  player_name: string;
  player_id: string | null; // A Bola id (not zerozero)
  score: number | null;
  raw_score?: number | null; // provider raw rating (Goal decimal); A Bola has none
  is_mvp: boolean;
  zz_player_id?: string | null; // matched zerozero id (set by sync)
};

type Player = {
  player_id: string;
  player_name: string;
  team_id: string;
  shirt_number: number | null;
  is_starter: boolean;
  order_index: number | null;
  reporter_score: number | null;
  reporter_is_mvp: boolean;
  reporter_linked: boolean;
  reporter_manual: boolean;
};

type Team = { id: string; name: string };

const SCORES = ["-", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"];

function order(a: Player, b: Player) {
  const oa = a.order_index ?? 9999;
  const ob = b.order_index ?? 9999;
  return oa - ob || Number(b.is_starter) - Number(a.is_starter);
}

export default function ReporterLinker({
  matchId,
  home,
  away,
  players,
  homeRatings,
  awayRatings,
}: {
  matchId: string;
  home: Team;
  away: Team;
  players: Player[];
  homeRatings: Rating[];
  awayRatings: Rating[];
}) {
  const router = useRouter();
  const supabase = createClient();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // Which player each entry was linked to THIS session (the stored ratings only
  // re-annotate on the next sync). Combined with the live reporter_linked state,
  // this lets an entry return to the leftover list when its player is unlinked.
  const [manualLinks, setManualLinks] = useState<Record<string, string>>({});

  async function call(
    fn: string,
    args: Record<string, unknown>,
    link?: { key: string; playerId: string },
  ) {
    setBusy(true);
    setErr(null);
    const { error } = await supabase.rpc(fn, args);
    setBusy(false);
    if (error) {
      setErr(error.message);
      return;
    }
    if (link) setManualLinks((p) => ({ ...p, [link.key]: link.playerId }));
    router.refresh();
  }

  function linkEntry(player: Player, teamId: string, entry: Rating, key: string) {
    call(
      "reporter_link",
      {
        p_match_id: matchId,
        p_player_id: player.player_id,
        p_score: entry.score,
        p_is_mvp: !!entry.is_mvp,
        p_abola_norm: norm(entry.player_name),
        p_team_id: teamId,
        p_abola_id: entry.player_id ?? null,
        // Carry the provider raw rating (Goal decimal) so the player shows up on
        // the goal-ratings page; null for A Bola, which has no raw value.
        p_raw_score: entry.raw_score ?? null,
      },
      { key, playerId: player.player_id },
    );
  }

  function setScore(player: Player, val: string) {
    call("reporter_link", {
      p_match_id: matchId,
      p_player_id: player.player_id,
      p_score: val === "-" ? null : Number(val),
      p_is_mvp: player.reporter_is_mvp ?? false,
      p_abola_norm: null,
      p_team_id: null,
      p_abola_id: null,
    });
  }

  function unlink(player: Player) {
    call("reporter_unlink", { p_match_id: matchId, p_player_id: player.player_id });
  }

  function section(team: Team, ratings: Rating[]) {
    const roster = players.filter((p) => p.team_id === team.id).sort(order);
    // An entry is a leftover unless the player it's linked to (this session, or
    // from the last sync) is CURRENTLY linked -- so unlinking a player frees its
    // A Bola entry back into the dropdown.
    const linkedIds = new Set(
      roster.filter((p) => p.reporter_linked).map((p) => p.player_id),
    );
    const leftovers = ratings
      .map((r, i) => ({ r, key: `${team.id}:${i}` }))
      .filter(({ r, key }) => {
        const pid = manualLinks[key] ?? r.zz_player_id;
        return !(pid && linkedIds.has(pid));
      });
    const linkedCount = roster.filter((p) => p.reporter_linked).length;

    return (
      <div style={{ marginBottom: 18 }} key={team.id}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h3 style={{ margin: "0 0 6px" }}>{team.name}</h3>
          <span className={`cov ${linkedCount === roster.length ? "ok" : "warn"}`}>
            {linkedCount}/{roster.length} linked
          </span>
        </div>

        {roster.map((p) => {
          const st = p.reporter_linked
            ? p.reporter_manual
              ? ["man", "✓ manual"]
              : ["auto", "✓ auto"]
            : ["miss", "⚠ unlinked"];
          return (
            <div className="linkrow" key={p.player_id}>
              <span className="num" style={{ width: 22, textAlign: "center" }}>
                {p.shirt_number ?? ""}
              </span>
              <span className="score" style={{ width: 24, textAlign: "center" }}>
                {p.reporter_linked ? p.reporter_score ?? "–" : ""}
              </span>
              <span className="pname">
                {p.player_name}
                {p.reporter_is_mvp && <span className="badge">MVP</span>}
              </span>
              <span className={`st ${st[0]}`}>{st[1]}</span>
              {!p.reporter_linked && leftovers.length > 0 && (
                <select
                  disabled={busy}
                  defaultValue=""
                  onChange={(e) => {
                    const lo = leftovers[Number(e.target.value)];
                    if (lo) linkEntry(p, team.id, lo.r, lo.key);
                  }}
                >
                  <option value="" disabled>
                    Link A Bola entry…
                  </option>
                  {leftovers.map((lo, i) => (
                    <option key={lo.key} value={i}>
                      {lo.r.player_name} ({lo.r.score ?? "–"})
                    </option>
                  ))}
                </select>
              )}
              <select
                disabled={busy}
                value=""
                onChange={(e) => e.target.value && setScore(p, e.target.value)}
              >
                <option value="">{p.reporter_linked ? "Edit score…" : "Set score…"}</option>
                {SCORES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
              {p.reporter_linked && (
                <button className="btn secondary" disabled={busy} onClick={() => unlink(p)}>
                  unlink
                </button>
              )}
            </div>
          );
        })}

        {leftovers.length > 0 && (
          <>
            <div className="subhead">A Bola entries not linked</div>
            {leftovers.map((lo) => {
              const free = roster.filter((p) => !p.reporter_linked);
              return (
                <div className="leftover" key={lo.key}>
                  <span style={{ flex: 1 }}>
                    {lo.r.player_name} <b>({lo.r.score ?? "–"})</b>
                    {lo.r.is_mvp ? " ★" : ""}
                  </span>
                  <select
                    disabled={busy}
                    defaultValue=""
                    onChange={(e) => {
                      const p = free.find((x) => x.player_id === e.target.value);
                      if (p) linkEntry(p, team.id, lo.r, lo.key);
                    }}
                  >
                    <option value="" disabled>
                      Link to player…
                    </option>
                    {free.map((p) => (
                      <option key={p.player_id} value={p.player_id}>
                        {p.player_name}
                      </option>
                    ))}
                  </select>
                </div>
              );
            })}
          </>
        )}
      </div>
    );
  }

  return (
    <>
      {err && (
        <p className="muted" style={{ color: "var(--red)", marginTop: 0 }}>
          {err}
        </p>
      )}
      {section(home, homeRatings)}
      {section(away, awayRatings)}
    </>
  );
}
