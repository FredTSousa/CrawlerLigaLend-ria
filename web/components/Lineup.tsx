import React from "react";

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

function StatIcons({ p }: { p: Player }) {
  const chips: React.ReactNode[] = [];
  if (p.goals > 0)
    chips.push(<span className="chip" title="Goals" key="g">⚽{count(p.goals)}</span>);
  if (p.assists > 0)
    chips.push(<span className="chip" title="Assists" key="a">🅰️{count(p.assists)}</span>);
  if (p.yellow_cards > 0)
    chips.push(<span className="chip" title="Yellow card" key="y">🟨{count(p.yellow_cards)}</span>);
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
  return <span className="icons">{chips}</span>;
}

function PlayerLine({ p }: { p: Player }) {
  return (
    <div className="pline">
      <span className="num">{p.shirt_number ?? ""}</span>
      <span className="pname">
        {p.player_name}
        {p.is_captain && <span className="badge">C</span>}
      </span>
      <StatIcons p={p} />
    </div>
  );
}

function TeamColumn({ name, rows }: { name: string; rows: Player[] }) {
  const starters = rows.filter((r) => r.is_starter);
  const subs = rows.filter((r) => !r.is_starter);
  return (
    <div className="team-col">
      <h3>{name}</h3>
      {starters.map((p) => (
        <PlayerLine key={p.player_id} p={p} />
      ))}
      {subs.length > 0 && <div className="subhead">Substitutes used</div>}
      {subs.map((p) => (
        <PlayerLine key={p.player_id} p={p} />
      ))}
    </div>
  );
}

export default function Lineup({
  home,
  away,
  players,
}: {
  home: Team;
  away: Team;
  players: Player[];
}) {
  const col = (teamId: string) =>
    players.filter((p) => p.team_id === teamId).sort(orderCmp);
  if (!players.length) {
    return <p className="muted">No player data yet — crawl this match.</p>;
  }
  return (
    <div className="lineup">
      <TeamColumn name={home.name} rows={col(home.id)} />
      <TeamColumn name={away.name} rows={col(away.id)} />
    </div>
  );
}
