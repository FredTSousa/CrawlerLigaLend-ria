import React from "react";
import StatEditor, { type EditableStat } from "./StatEditor";

type MatchEvent = {
  event_type: string;
  minute: number;
  extra_time: number | null;
};

type Player = {
  player_id: string;
  player_name: string;
  team_id: string;
  order_index: number | null;
  shirt_number: number | null;
  is_captain: boolean;
  is_starter: boolean;
  entered_min: number | null;
  left_min: number | null;
  goals: number;
  assists: number;
  yellow_cards: number;
  red_card: boolean;
  own_goals: number;
  penalties_scored: number;
  penalties_missed: number;
  penalties_defended: number;
  did_not_play: boolean;
  reporter_score: number | null;
  reporter_is_mvp: boolean;
  reporter_linked: boolean;
  stat_overrides?: Partial<Record<EditableStat, number>> | null;
  stat_crawled?: Partial<Record<EditableStat, number>> | null;
  events?: MatchEvent[];
};

type Team = { id: string; name: string };

function orderCmp(a: Player, b: Player) {
  const oa = a.order_index ?? 9999;
  const ob = b.order_index ?? 9999;
  if (oa !== ob) return oa - ob;
  return (
    Number(b.is_starter) - Number(a.is_starter) ||
    (a.shirt_number ?? 99) - (b.shirt_number ?? 99)
  );
}

function count(n: number) {
  return n > 1 ? <span className="n">{n}</span> : null;
}

function fmtMin(minute: number, extra: number | null): string {
  return extra ? `${minute}+${extra}'` : `${minute}'`;
}

const EVENT_CHIP: Record<string, { icon: string; cls: string; title: string }> = {
  goal:             { icon: "⚽",    cls: "chip",     title: "Goal" },
  own_goal:         { icon: "OG",    cls: "chip og",  title: "Own goal" },
  penalty_scored:   { icon: "PK ⚽", cls: "chip pen", title: "Penalty scored" },
  assist:           { icon: "👟",    cls: "chip",     title: "Assist" },
  yellow_card:      { icon: "🟨",    cls: "chip",     title: "Yellow card" },
  red_card:         { icon: "🟥",    cls: "chip",     title: "Red card" },
  penalty_missed:   { icon: "PK ✗", cls: "chip pen", title: "Penalty missed" },
  penalty_defended: { icon: "PK 🧤", cls: "chip pen", title: "Penalty saved" },
  sub_in:           { icon: "🔼",    cls: "chip in",  title: "Subbed in" },
  sub_out:          { icon: "🔽",    cls: "chip out", title: "Subbed out" },
};

// Event types whose timeline chips are replaced by a count chip when the
// matching stat was corrected by hand (the crawled events are the wrong ones).
const OVERRIDE_EVENT: Record<EditableStat, string> = {
  goals: "goal",
  assists: "assist",
  yellow_cards: "yellow_card",
};

function StatIcons({ p }: { p: Player }) {
  const chips: React.ReactNode[] = [];
  const over = p.stat_overrides ?? {};
  const overKeys = Object.keys(over) as EditableStat[];
  const hiddenEvents = new Set(overKeys.map((k) => OVERRIDE_EVENT[k]));

  if (p.events && p.events.length > 0) {
    const sorted = [...p.events].sort(
      (a, b) => a.minute - b.minute || (a.extra_time ?? 0) - (b.extra_time ?? 0),
    );
    sorted.forEach((ev, i) => {
      const meta = EVENT_CHIP[ev.event_type];
      if (!meta || hiddenEvents.has(ev.event_type)) return;
      chips.push(
        <span className={meta.cls} title={meta.title} key={`${ev.event_type}-${i}`}>
          {ev.event_type === "sub_in" || ev.event_type === "sub_out"
            ? `${meta.icon} ${fmtMin(ev.minute, ev.extra_time)}`
            : `${meta.icon} (${fmtMin(ev.minute, ev.extra_time)})`}
        </span>,
      );
    });
    // Hand-corrected stats: the minute-by-minute events are untrustworthy, so
    // show the corrected count instead.
    for (const k of overKeys) {
      const n = p[k];
      if (n <= 0) continue;
      const meta = EVENT_CHIP[OVERRIDE_EVENT[k]];
      chips.push(
        <span className={`${meta.cls} edited`} title={`${meta.title} (edited by hand)`} key={`ov-${k}`}>
          {meta.icon}{count(n)}
        </span>,
      );
    }
  } else {
    // Fallback when event timeline hasn't been crawled yet: show counts only.
    if (p.goals > 0)
      chips.push(<span className={`chip${"goals" in over ? " edited" : ""}`} title="Goals" key="g">⚽{count(p.goals)}</span>);
    if (p.assists > 0)
      chips.push(<span className={`chip${"assists" in over ? " edited" : ""}`} title="Assists" key="a">👟{count(p.assists)}</span>);
    if (p.yellow_cards > 0)
      chips.push(<span className={`chip${"yellow_cards" in over ? " edited" : ""}`} title="Yellow card" key="y">🟨{count(p.yellow_cards)}</span>);
    if (p.red_card)
      chips.push(<span className="chip" title="Red card" key="r">🟥</span>);
    if (p.own_goals > 0)
      chips.push(<span className="chip og" title="Own goal" key="og">OG{p.own_goals > 1 ? ` ${p.own_goals}` : ""}</span>);
    if (p.penalties_scored > 0)
      chips.push(<span className="chip pen" title="Penalty scored" key="ps">PK ⚽{p.penalties_scored > 1 ? ` ${p.penalties_scored}` : ""}</span>);
    if (p.penalties_missed > 0)
      chips.push(<span className="chip pen" title="Penalty missed" key="pm">PK ✗{p.penalties_missed > 1 ? ` ${p.penalties_missed}` : ""}</span>);
    if (p.penalties_defended > 0)
      chips.push(<span className="chip pen" title="Penalty saved" key="pd">PK 🧤{p.penalties_defended > 1 ? ` ${p.penalties_defended}` : ""}</span>);
    if (p.entered_min != null)
      chips.push(<span className="chip in" title="Subbed in" key="in">🔼 {p.entered_min}&#39;</span>);
    if (p.left_min != null)
      chips.push(<span className="chip out" title="Subbed out" key="out">🔽 {p.left_min}&#39;</span>);
  }

  return <span className="icons">{chips}</span>;
}

// Reporter (A Bola) score shown in front of the name. Only rendered once a
// reporter scrape has run for the match (`show`), so a missing rating reads as
// a real gap (⚠) rather than "not fetched yet".
function RepScore({ p, show }: { p: Player; show: boolean }) {
  if (!show) return null;
  if (p.did_not_play) return null;
  if (!p.reporter_linked) {
    return (
      <span className="repchip miss" title="No reporter rating linked">
        ⚠
      </span>
    );
  }
  const s = p.reporter_score;
  const band = s == null ? "none" : s >= 7 ? "hi" : s <= 4 ? "lo" : "mid";
  return (
    <span
      className={`repchip ${band}${p.reporter_is_mvp ? " mvp" : ""}`}
      title={`Reporter rating${p.reporter_is_mvp ? " · MVP" : ""}`}
    >
      {p.reporter_is_mvp ? "★" : ""}
      {s == null ? "–" : s}
    </span>
  );
}

function PlayerLine({ p, rep, matchId }: { p: Player; rep: boolean; matchId?: string }) {
  return (
    <div className="pline">
      <span className="num">{p.shirt_number ?? ""}</span>
      <RepScore p={p} show={rep} />
      <span className="pname">
        {p.player_name}
        {p.is_captain && <span className="badge">C</span>}
      </span>
      <StatIcons p={p} />
      {matchId && (
        <StatEditor
          matchId={matchId}
          playerId={p.player_id}
          values={{ goals: p.goals, assists: p.assists, yellow_cards: p.yellow_cards }}
          overrides={p.stat_overrides ?? {}}
          crawled={p.stat_crawled ?? {}}
        />
      )}
    </div>
  );
}

function TeamColumn({
  name,
  rows,
  rep,
  matchId,
}: {
  name: string;
  rows: Player[];
  rep: boolean;
  matchId?: string;
}) {
  const starters = rows.filter((r) => r.is_starter);
  const subs = rows.filter((r) => !r.is_starter);
  return (
    <div className="team-col">
      <h3>{name}</h3>
      {starters.map((p) => (
        <PlayerLine key={p.player_id} p={p} rep={rep} matchId={matchId} />
      ))}
      {subs.length > 0 && <div className="subhead">Substitutes used</div>}
      {subs.map((p) => (
        <PlayerLine key={p.player_id} p={p} rep={rep} matchId={matchId} />
      ))}
    </div>
  );
}

export default function Lineup({
  home,
  away,
  players,
  reporterFetched = false,
  matchId,
}: {
  home: Team;
  away: Team;
  players: Player[];
  reporterFetched?: boolean;
  // Enables the per-player ✎ stat correction.
  matchId?: string;
}) {
  const col = (teamId: string) =>
    players.filter((p) => p.team_id === teamId).sort(orderCmp);
  if (!players.length) {
    return <p className="muted">No player data yet — crawl this match.</p>;
  }
  return (
    <div className="lineup">
      <TeamColumn name={home.name} rows={col(home.id)} rep={reporterFetched} matchId={matchId} />
      <TeamColumn name={away.name} rows={col(away.id)} rep={reporterFetched} matchId={matchId} />
    </div>
  );
}
