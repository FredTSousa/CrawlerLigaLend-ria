-- Migration: a "watch" flag so the site can mark matches for live watching.
-- A single watcher process follows every match with watch=true (and not final).
-- Run once in the Supabase SQL editor. Safe to re-run.

alter table public.matches
    add column if not exists watch boolean not null default false;

-- Let the approved user create/flag matches to watch from the website
-- (reads are already gated by the existing select policies; service_role,
-- used by the crawler, bypasses RLS).
drop policy if exists matches_insert on public.matches;
create policy matches_insert on public.matches
    for insert to authenticated
    with check (public.is_allowed());

drop policy if exists matches_update on public.matches;
create policy matches_update on public.matches
    for update to authenticated
    using (public.is_allowed())
    with check (public.is_allowed());
