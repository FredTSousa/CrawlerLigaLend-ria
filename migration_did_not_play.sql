-- Add did_not_play flag to match_players so bench players who never entered
-- the match are stored (for live "might come in" display) rather than dropped.
alter table match_players
  add column if not exists did_not_play boolean not null default false;
