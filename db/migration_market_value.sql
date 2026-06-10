-- ============================================================================
-- migration_market_value.sql  (additive; safe to re-run)
-- ============================================================================
-- Transfermarkt "Valor de mercado" (market value) per player, captured during a
-- Full / players-only squad sync. Stored in THOUSANDS of euros (so "8,00 M €"
-- is 8000 and "600 mil €" is 600) to keep it a compact integer. NULL when the
-- player has no listed value or hasn't been enriched yet.
-- ============================================================================

alter table public.players
    add column if not exists market_value_k integer;   -- market value in thousands of EUR
