-- Migration: support live ("ongoing") matches.
-- Run once in the Supabase SQL editor. Safe to re-run.

alter table public.matches
    add column if not exists status     text not null default 'scheduled',
    add column if not exists minute     text,
    add column if not exists kickoff_at timestamptz;

-- status: 'scheduled' -> 'live' -> 'final' (also 'postponed').
-- minute: live clock for display, e.g. '67'', 'HT', '90+2''  (null otherwise).
-- kickoff_at: scheduled start time.

comment on column public.matches.status is 'scheduled | live | final | postponed';
