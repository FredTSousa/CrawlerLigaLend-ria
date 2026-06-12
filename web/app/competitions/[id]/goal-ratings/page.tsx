import Link from "next/link";
import { createClient } from "@/lib/supabase/server";

export const dynamic = "force-dynamic";

// Default raw-score -> reporter score mapping. Mirrors rating_map.py / RatingSourceSettings.
const DEFAULT_MAPPING: Record<string, number> = {
  "0-5": 4,
  "5-6": 5,
  "6-6.5": 6,
  "6.5-7": 7,
  "7-7.5": 8,
  "7.5-8": 9,
  "8-10": 10,
};

type Bucket = { lo: number; hi: number; score: number };

function buckets(mapping: Record<string, number> | null): Bucket[] {
  const m = mapping && Object.keys(mapping).length ? mapping : DEFAULT_MAPPING;
  return Object.entries(m)
    .map(([range, score]) => {
      const [lo, hi] = range.split("-").map(Number);
      return { lo, hi, score: Number(score) };
    })
    .filter((b) => Number.isFinite(b.lo) && Number.isFinite(b.hi) && Number.isFinite(b.score))
    .sort((a, b) => a.lo - b.lo);
}

// Percentile is informational only (shown to help judge where to set ranges); it
// is NOT used for conversion — the reporter score is mapped from the raw value.
function percentile(raw: number, dist: number[]): number {
  if (!dist.length) return 0;
  const le = dist.filter((d) => d <= raw).length;
  return (100 * le) / dist.length;
}

// Map a RAW Goal rating to its reporter score via the configured raw-score ranges.
function mapScore(raw: number, bs: Bucket[]): number | null {
  if (!bs.length) return null;
  const topHi = bs[bs.length - 1].hi;
  for (const b of bs) {
    if ((raw >= b.lo && raw < b.hi) || (b.hi >= topHi && raw === b.hi)) return b.score;
  }
  return null;
}

export default async function GoalRatingsPage({ params }: { params: { id: string } }) {
  const supabase = createClient();
  const id = params.id;

  const { data: comp } = await supabase
    .from("competitions")
    .select("id, name, full_name, slug, rating_source, rating_mapping")
    .eq("id", id)
    .maybeSingle();

  if (!comp) {
    return (
      <div className="panel">
        <h2>Competition not found</h2>
        <Link href="/competitions">← all competitions</Link>
      </div>
    );
  }

  const compLabel = comp.full_name || comp.name || comp.slug;
  const bs = buckets(comp.rating_mapping as any);

  // Match ids in this competition, then their stored raw Goal ratings.
  const { data: matchRows } = await supabase
    .from("matches")
    .select("id")
    .eq("competition_id", id);
  const matchIds = ((matchRows ?? []) as any[]).map((m) => m.id);

  let rows: any[] = [];
  if (matchIds.length) {
    const { data } = await supabase
      .from("match_player_details")
      .select(
        "match_id, player_name, team_name, played_on, round, reporter_raw_score, reporter_score, reporter_is_mvp",
      )
      .in("match_id", matchIds)
      .not("reporter_raw_score", "is", null)
      .limit(5000);
    rows = (data ?? []) as any[];
  }

  const dist = rows
    .map((r) => Number(r.reporter_raw_score))
    .filter((v) => Number.isFinite(v));

  const enriched = rows
    .map((r) => {
      const raw = Number(r.reporter_raw_score);
      return { ...r, raw, pct: percentile(raw, dist), mapped: mapScore(raw, bs) };
    })
    .sort((a, b) => b.raw - a.raw);

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2 style={{ marginBottom: 4 }}>Goal ratings — {compLabel}</h2>
            <div className="muted" style={{ fontSize: 13 }}>
              {dist.length} stored rating{dist.length === 1 ? "" : "s"}
              {dist.length > 0 && (
                <> · raw min {Math.min(...dist).toFixed(2)} · max {Math.max(...dist).toFixed(2)} ·
                  avg {(dist.reduce((a, b) => a + b, 0) / dist.length).toFixed(2)}</>
              )}{" "}
              · percentile shown for context only (score is mapped from the raw value)
            </div>
          </div>
          <Link href={`/competitions/${id}`} className="round-chip">← competition</Link>
        </div>

        {/* Mapping legend — raw score range → reporter score */}
        <div className="row" style={{ gap: 6, marginTop: 10, flexWrap: "wrap" }}>
          {bs.map((b) => (
            <span key={`${b.lo}-${b.hi}`} className="round-chip" title="raw range → score">
              {b.lo}–{b.hi} → {b.score}
            </span>
          ))}
        </div>
      </div>

      <div className="panel">
        {enriched.length ? (
          <table>
            <thead>
              <tr>
                <th>Player</th>
                <th>Team</th>
                <th>Round</th>
                <th>Date</th>
                <th className="num">Raw</th>
                <th className="num">Percentile</th>
                <th className="num">Mapped</th>
              </tr>
            </thead>
            <tbody>
              {enriched.map((r, i) => (
                <tr key={`${r.match_id}-${r.player_name}-${i}`}>
                  <td>
                    {r.player_name}
                    {r.reporter_is_mvp ? <span className="live-badge" style={{ marginLeft: 6 }}>MVP</span> : null}
                  </td>
                  <td className="muted">{r.team_name ?? "—"}</td>
                  <td className="muted">{r.round ?? "—"}</td>
                  <td className="muted">{r.played_on ?? "—"}</td>
                  <td className="num">{r.raw.toFixed(2)}</td>
                  <td className="num">{r.pct.toFixed(0)}%</td>
                  <td className="num score">{r.mapped ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted">
            No Goal ratings stored yet for this competition. Set the source to GOAL with a
            fixtures URL, then “Fetch reporter score” on a finished match.
          </p>
        )}
      </div>
    </>
  );
}
