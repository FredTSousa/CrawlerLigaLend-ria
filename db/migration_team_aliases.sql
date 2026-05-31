-- Migration: team name aliases (alternative names A Bola uses for a club).
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- zerozero and A Bola don't always agree on a club's name ('AFS' vs 'Aves
-- SAD'). reporter_sync loads these into abola.TEAM_ALIAS_NAMES so A Bola is
-- SEARCHED with the right name, not just filtered by it. Managed from the
-- /team-aliases page.

create table if not exists public.team_aliases (
    id         bigint generated always as identity primary key,
    team_id    text not null references public.teams(id) on delete cascade,
    alias      text not null,                 -- a name A Bola may use, e.g. 'Aves SAD'
    created_at timestamptz not null default now(),
    unique (team_id, alias)
);

create index if not exists team_aliases_team_idx on public.team_aliases (team_id);

alter table public.team_aliases enable row level security;

drop policy if exists team_aliases_read on public.team_aliases;
create policy team_aliases_read on public.team_aliases
    for select to authenticated using (public.is_allowed());

drop policy if exists team_aliases_insert on public.team_aliases;
create policy team_aliases_insert on public.team_aliases
    for insert to authenticated with check (public.is_allowed());

drop policy if exists team_aliases_delete on public.team_aliases;
create policy team_aliases_delete on public.team_aliases
    for delete to authenticated using (public.is_allowed());
