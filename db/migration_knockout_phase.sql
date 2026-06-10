-- ============================================================================
-- migration_knockout_phase.sql  (additive; safe to re-run)
-- ============================================================================
-- Cup / knockout competitions (e.g. the FIFA World Cup) are not a flat list of
-- numbered jornadas: after the group stage they run named phases (Oitavos,
-- Quartos, Meias-Finais, Final, ...) addressed by `fase`, not `jornada_in`.
--
-- The crawler maps each knockout phase to a `round` number that continues after
-- the group jornadas (so the existing round-based UI keeps working) and stores
-- the human-readable phase name here. League rounds leave this NULL.
-- ============================================================================

alter table public.matches
    add column if not exists phase text;   -- e.g. 'Oitavos-de-Final', 'Final' (NULL for league rounds)
