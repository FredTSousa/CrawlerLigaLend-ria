export default function StatusBadge({
  status,
  minute,
}: {
  status?: string | null;
  minute?: string | null;
}) {
  if (status === "live") {
    return <span className="live-badge">LIVE{minute ? ` ${minute}` : ""}</span>;
  }
  if (status === "scheduled") {
    return <span className="sched-badge">Scheduled</span>;
  }
  if (status === "postponed") {
    return <span className="sched-badge">Postponed</span>;
  }
  if (status === "final") {
    return <span className="ft-badge">Full time</span>;
  }
  return null;
}
