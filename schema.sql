-- Run this in Supabase → SQL Editor
create table if not exists public.banned_words (
  id         bigint generated always as identity primary key,
  chat_id    bigint      not null,
  word       text        not null,
  added_by   bigint,
  created_at timestamptz not null default now(),
  unique (chat_id, word)
);

create index if not exists banned_words_chat_idx on public.banned_words (chat_id);

-- Lock the table: RLS on with no policies = only the service_role key (the bot) can read/write
alter table public.banned_words enable row level security;

-- Deletion log (used by /status)
create table if not exists public.delete_log (
  id         bigint generated always as identity primary key,
  chat_id    bigint      not null,
  user_id    bigint,
  word       text,
  created_at timestamptz not null default now()
);
create index if not exists delete_log_chat_time_idx on public.delete_log (chat_id, created_at);
alter table public.delete_log enable row level security;
