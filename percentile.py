#!/usr/bin/env python3
"""
Percentile -> score conversion for raw reporter ratings (Goal.com).

A provider like Goal.com reports a raw rating (a decimal, e.g. 7.55). To map it
onto the 0-10 integer the rest of the app uses, we rank it against the
distribution of raw ratings stored for the *same competition*, then translate
the resulting percentile through a per-competition, manually-configured mapping:

    {"0-20": 4, "20-40": 5, "40-50": 6, "50-70": 7,
     "70-85": 8, "85-95": 9, "95-100": 10}

Keys are percentile ranges "lo-hi"; a percentile P falls in [lo, hi) (the top
bucket is inclusive of 100). The mapping is always supplied by the caller from
the competition config; DEFAULT_MAPPING is only the fallback when a competition
has none set yet. Nothing here is hardcoded to a particular competition.
"""

from __future__ import annotations

DEFAULT_MAPPING: dict[str, int] = {
    "0-20": 4,
    "20-40": 5,
    "40-50": 6,
    "50-70": 7,
    "70-85": 8,
    "85-95": 9,
    "95-100": 10,
}


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def percentile(raw, distribution) -> float | None:
    """P(raw) = 100 * (count of d <= raw) / total, over `distribution` (a list
    of raw ratings). Returns None when raw isn't numeric or the distribution is
    empty (caller decides what to do with an unrankable value)."""
    r = _to_float(raw)
    if r is None:
        return None
    vals = [v for v in (_to_float(d) for d in distribution) if v is not None]
    if not vals:
        return None
    le = sum(1 for v in vals if v <= r)
    return 100.0 * le / len(vals)


def _parse_buckets(mapping: dict) -> list[tuple[float, float, int]]:
    """Normalise a {"lo-hi": score} mapping into sorted (lo, hi, score) tuples.
    Tolerant of spaces, "%", and "lo-100"/"95-100" top bounds."""
    out: list[tuple[float, float, int]] = []
    for key, score in (mapping or {}).items():
        k = str(key).replace("%", "").strip()
        parts = k.split("-")
        if len(parts) != 2:
            continue
        lo, hi = _to_float(parts[0]), _to_float(parts[1])
        sc = _to_float(score)
        if lo is None or hi is None or sc is None:
            continue
        out.append((lo, hi, int(round(sc))))
    out.sort(key=lambda t: t[0])
    return out


def score_for_percentile(p: float, mapping: dict | None) -> int | None:
    """Translate a percentile (0-100) through the bucket mapping. A percentile
    sits in [lo, hi); the highest bucket is inclusive of its upper bound so 100
    maps. Returns None if no bucket covers it."""
    if p is None:
        return None
    buckets = _parse_buckets(mapping or DEFAULT_MAPPING)
    if not buckets:
        return None
    top_hi = buckets[-1][1]
    for lo, hi, sc in buckets:
        if lo <= p < hi or (hi >= top_hi and p == hi):
            return sc
    return None


def convert(raw, distribution, mapping: dict | None = None) -> int | None:
    """Raw rating -> mapped 0-10 score: rank `raw` in `distribution`, then run
    the percentile through `mapping` (DEFAULT_MAPPING when None). Returns None
    when raw is unrankable (non-numeric / empty distribution)."""
    p = percentile(raw, distribution)
    if p is None:
        return None
    return score_for_percentile(p, mapping)


if __name__ == "__main__":  # tiny smoke test
    dist = [6, 6.5, 7, 7.55, 8]
    for r in dist:
        print(r, "->", f"{percentile(r, dist):.0f}%", "=>",
              convert(r, dist, DEFAULT_MAPPING))
