// Normalize a player name for matching/alias keys.
//
// CRITICAL: this must produce the SAME string as abola._norm() in abola.py
// (lowercase, strip accents, drop everything but [a-z0-9]). reporter_aliases
// rows are keyed by this value; the Python matcher looks them up with
// abola._norm(). If the two drift, alias lookups silently miss.
export function norm(s: string): string {
  return (s || "")
    .normalize("NFKD")
    .replace(/\p{Diacritic}/gu, "")
    .toLowerCase()
    .replace(/[^a-z0-9]/g, "");
}
