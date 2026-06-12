#!/usr/bin/env python3
"""
Raw reporter rating -> 0-10 score mapping (Goal.com).

A provider like Goal.com reports a raw rating (a decimal, e.g. 7.55). Each
competition (= one season/edition) configures, by hand, which RAW score ranges
map to which 0-10 reporter score:

    {"0-5": 4, "5-6": 5, "6-6.5": 6, "6.5-7": 7,
     "7-7.5": 8, "7.5-8": 9, "8-10": 10}

Keys are raw-score ranges "lo-hi"; a raw value r falls in [lo, hi) (the top
bucket is inclusive of its upper bound). Conversion is a pure per-rating lookup
-- no distribution / percentile maths is involved (the viewer page still shows
percentiles, but only as analytical context). The mapping is always supplied by
the caller from the competition config; DEFAULT_MAPPING is only the fallback
when a competition has none set yet. Nothing is hardcoded to a competition.
"""

from __future__ import annotations

DEFAULT_MAPPING: dict[str, int] = {
    "0-5": 4,
    "5-6": 5,
    "6-6.5": 6,
    "6.5-7": 7,
    "7-7.5": 8,
    "7.5-8": 9,
    "8-10": 10,
}


def _to_float(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _parse_buckets(mapping: dict) -> list[tuple[float, float, int]]:
    """Normalise a {"lo-hi": score} mapping into sorted (lo, hi, score) tuples.
    Tolerant of spaces and decimals ('6.5-7')."""
    out: list[tuple[float, float, int]] = []
    for key, score in (mapping or {}).items():
        parts = str(key).strip().split("-")
        if len(parts) != 2:
            continue
        lo, hi, sc = _to_float(parts[0]), _to_float(parts[1]), _to_float(score)
        if lo is None or hi is None or sc is None:
            continue
        out.append((lo, hi, int(round(sc))))
    out.sort(key=lambda t: t[0])
    return out


def convert(raw, mapping: dict | None = None) -> int | None:
    """Map a raw rating to its 0-10 score via the configured raw-score ranges
    (DEFAULT_MAPPING when None). A raw value sits in [lo, hi); the highest bucket
    is inclusive of its upper bound. Returns None when raw isn't numeric or no
    bucket covers it (the competition's ranges should span the rating scale)."""
    r = _to_float(raw)
    if r is None:
        return None
    buckets = _parse_buckets(mapping or DEFAULT_MAPPING)
    if not buckets:
        return None
    top_hi = buckets[-1][1]
    for lo, hi, sc in buckets:
        if lo <= r < hi or (hi >= top_hi and r == hi):
            return sc
    return None


if __name__ == "__main__":  # tiny smoke test
    for r in (4.7, 5.65, 6.05, 7.0, 7.55, 8.1):
        print(r, "->", convert(r, DEFAULT_MAPPING))
