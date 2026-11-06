-- AI Job Scanner - Supabase schema.
-- Run once in Supabase: Dashboard -> SQL Editor -> New query -> paste -> Run.
--
-- Who writes what:
--   scanner (GitHub Actions, service_role key - bypasses RLS): offers, runs, queue, skipped
--   web app (logged-in user):                                  applications, settings
-- Row Level Security: only authenticated users can read anything; turn OFF sign-ups in
-- Authentication -> Providers -> Email so the only account is yours.

create table if not exists offers (
  id                   text primary key,
  source               text,
  first_seen           timestamptz,
  title                text,
  company              text,
  location             text,
  url                  text,
  search_keyword       text,
  score                int,
  verdict              text,
  seniority            text,
  one_line_reason      text,
  strengths            jsonb,
  gaps                 jsonb,
  notified             boolean default false,
  role_category        text,
  industry             text,
  work_mode            text,
  contract_types       jsonb,
  salary_min           numeric,
  salary_max           numeric,
  salary_currency      text,
  salary_period        text,
  salary_type          text,
  salary_min_pln_month numeric,
  salary_max_pln_month numeric,
  years_required       numeric,
  english_level        text,
  polish_required      boolean,
  required_tools       jsonb,
  nice_to_have_tools   jsonb,
  model                text,
  synced_at            timestamptz default now()
);
create index if not exists offers_first_seen on offers (first_seen desc);
create index if not exists offers_score on offers (score desc);

create table if not exists runs (
  run_id          bigint primary key,
  started_at      timestamptz,
  finished_at     timestamptz,
  window_hours    int,
  cards_found     int,
  filtered_title  int,
  new_offers      int,
  triaged_out     int,
  scored          int,
  failed          int,
  notified        int,
  queued          int,
  stopped_by_time int,
  search_seconds  int,
  triage_seconds  int,
  score_seconds   int,
  gemini_calls    int,
  gemini_errors   int,
  tokens_in       int,
  tokens_out      int,
  sources         jsonb,
  model           text
);

-- current queue (replaced on every sync)
create table if not exists queue (
  id        text primary key,
  source    text,
  title     text,
  company   text,
  location  text,
  url       text,
  queued_at timestamptz
);

-- offers dropped before scoring (title filter / duplicate / AI pre-filter), last 30 days
create table if not exists skipped (
  id         text primary key,
  source     text,
  title      text,
  company    text,
  location   text,
  url        text,
  reason     text,
  skipped_at timestamptz
);

-- your application tracker (written by the web app)
create table if not exists applications (
  offer_id     text primary key references offers (id) on delete cascade,
  status       text not null default 'saved'
               check (status in ('saved', 'applied', 'interview', 'offer', 'rejected', 'not interested')),
  applied_on   date,
  follow_up_on date,
  notes        text,
  updated_at   timestamptz default now()
);

-- search settings edited in the web app; the scanner reads them at the start of every run
create table if not exists settings (
  key        text primary key
             check (key in ('keywords', 'locations', 'include_title_keywords', 'exclude_title_keywords',
                            'min_score_to_notify')),
  value      jsonb not null,
  updated_at timestamptz default now()
);

-- the settings the scanner actually used in its last run (written by the scanner, shown in the app)
create table if not exists effective_settings (
  key        text primary key,
  value      jsonb not null,
  updated_at timestamptz default now()
);

alter table offers       enable row level security;
alter table runs         enable row level security;
alter table queue        enable row level security;
alter table skipped      enable row level security;
alter table applications enable row level security;
alter table settings     enable row level security;
alter table effective_settings enable row level security;

-- read: any logged-in user (sign-ups are disabled, so that's only you)
drop policy if exists "read offers" on offers;             create policy "read offers"   on offers   for select to authenticated using (true);
drop policy if exists "read runs" on runs;                 create policy "read runs"     on runs     for select to authenticated using (true);
drop policy if exists "read queue" on queue;               create policy "read queue"    on queue    for select to authenticated using (true);
drop policy if exists "read skipped" on skipped;           create policy "read skipped"  on skipped  for select to authenticated using (true);
drop policy if exists "read effective" on effective_settings; create policy "read effective" on effective_settings for select to authenticated using (true);
-- read + write: your own data
drop policy if exists "own applications" on applications;  create policy "own applications" on applications for all to authenticated using (true) with check (true);
drop policy if exists "own settings" on settings;          create policy "own settings"     on settings     for all to authenticated using (true) with check (true);
