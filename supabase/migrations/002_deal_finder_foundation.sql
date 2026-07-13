create extension if not exists pgcrypto;

create table if not exists public.listing_sources (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    source_type text not null,
    base_url text,
    is_enabled boolean not null default true,
    scan_frequency_minutes integer,
    last_successful_scan_at timestamptz,
    last_error text,
    configuration jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create table if not exists public.listings (
    id uuid primary key default gen_random_uuid(),
    property_id uuid references public.properties(id) on delete set null,
    source_id uuid references public.listing_sources(id) on delete set null,
    external_listing_id text,
    source_url text not null,
    source_url_normalized text generated always as (lower(regexp_replace(source_url, '/+$', ''))) stored,
    title text,
    address text,
    city text,
    asking_price numeric,
    surface_m2 numeric,
    property_type text,
    listing_status text not null default 'active',
    first_seen_at timestamptz not null default timezone('utc', now()),
    last_seen_at timestamptz not null default timezone('utc', now()),
    is_active boolean not null default true,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create unique index if not exists uq_listings_source_external_id
on public.listings (source_id, external_listing_id)
where external_listing_id is not null;

create unique index if not exists uq_listings_source_url_normalized
on public.listings (source_url_normalized);

create index if not exists idx_listings_source_id on public.listings (source_id);
create index if not exists idx_listings_city on public.listings (city);
create index if not exists idx_listings_active on public.listings (is_active);

create table if not exists public.listing_snapshots (
    id uuid primary key default gen_random_uuid(),
    listing_id uuid not null references public.listings(id) on delete cascade,
    observed_at timestamptz not null default timezone('utc', now()),
    asking_price numeric,
    listing_status text,
    title text,
    description text,
    surface_m2 numeric,
    features jsonb not null default '{}'::jsonb,
    content_hash text not null,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_listing_snapshots_listing_id on public.listing_snapshots (listing_id);
create index if not exists idx_listing_snapshots_observed_at on public.listing_snapshots (observed_at desc);
create index if not exists idx_listing_snapshots_content_hash on public.listing_snapshots (content_hash);

create table if not exists public.scan_runs (
    id uuid primary key default gen_random_uuid(),
    source_id uuid references public.listing_sources(id) on delete set null,
    started_at timestamptz not null default timezone('utc', now()),
    completed_at timestamptz,
    status text,
    items_found integer not null default 0,
    items_new integer not null default 0,
    items_changed integer not null default 0,
    error_message text,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_scan_runs_source_id on public.scan_runs (source_id);
create index if not exists idx_scan_runs_started_at on public.scan_runs (started_at desc);

create table if not exists public.deal_candidates (
    id uuid primary key default gen_random_uuid(),
    listing_id uuid references public.listings(id) on delete set null,
    property_id uuid references public.properties(id) on delete set null,
    investment_score integer,
    hidden_value_score integer,
    priority text,
    reasons jsonb not null default '[]'::jsonb,
    detected_at timestamptz not null default timezone('utc', now()),
    reviewed_at timestamptz,
    review_status text not null default 'new',
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_deal_candidates_listing_id on public.deal_candidates (listing_id);
create index if not exists idx_deal_candidates_review_status on public.deal_candidates (review_status);
create index if not exists idx_deal_candidates_priority on public.deal_candidates (priority);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$;

drop trigger if exists trg_listing_sources_set_updated_at on public.listing_sources;
create trigger trg_listing_sources_set_updated_at
before update on public.listing_sources
for each row execute function public.set_updated_at();

drop trigger if exists trg_listings_set_updated_at on public.listings;
create trigger trg_listings_set_updated_at
before update on public.listings
for each row execute function public.set_updated_at();

alter table public.listing_sources enable row level security;
alter table public.listings enable row level security;
alter table public.listing_snapshots enable row level security;
alter table public.scan_runs enable row level security;
alter table public.deal_candidates enable row level security;