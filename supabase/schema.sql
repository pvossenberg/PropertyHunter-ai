create extension if not exists pgcrypto;

create table if not exists public.properties (
    id uuid primary key default gen_random_uuid(),
    source_url text,
    title text,
    address text,
    city text,
    country text,
    asking_price numeric,
    asking_price_status text not null default 'unknown',
    asking_price_text text,
    listed_since date,
    days_on_market integer,
    listing_status text not null default 'unknown',
    original_asking_price numeric,
    current_asking_price numeric,
    price_reduction_count integer not null default 0,
    last_price_reduction_date date,
    total_price_reduction_amount numeric,
    total_price_reduction_percentage numeric,
    listing_history_source text,
    listing_history_confidence text not null default 'unknown',
    surface_m2 numeric,
    price_per_m2 numeric,
    annual_rent numeric,
    property_type text,
    current_use text,
    zoning text,
    description text,
    raw_extracted_data jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_properties_source_url on public.properties (source_url);
create index if not exists idx_properties_address on public.properties (address);
create index if not exists idx_properties_created_at on public.properties (created_at desc);

create table if not exists public.analyses (
    id uuid primary key default gen_random_uuid(),
    property_id uuid not null references public.properties(id) on delete cascade,
    property_summary text,
    investment_score integer,
    score_breakdown jsonb not null default '{}'::jsonb,
    analysis_confidence_score integer,
    data_quality_warnings jsonb not null default '[]'::jsonb,
    strengths jsonb not null default '[]'::jsonb,
    risks jsonb not null default '[]'::jsonb,
    missing_information jsonb not null default '[]'::jsonb,
    assumptions jsonb not null default '[]'::jsonb,
    recommendation text,
    next_actions jsonb not null default '[]'::jsonb,
    raw_analysis jsonb not null,
    model_name text,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_analyses_property_id on public.analyses (property_id);
create index if not exists idx_analyses_created_at on public.analyses (created_at desc);

create table if not exists public.transactions (
    id uuid primary key default gen_random_uuid(),
    property_id uuid not null references public.properties(id) on delete cascade,
    analysis_id uuid references public.analyses(id) on delete set null,
    transaction_date date,
    transaction_type text not null default 'unknown',
    transaction_price numeric,
    price_status text not null default 'unknown',
    buyer_type text,
    seller_type text,
    source text,
    source_url text,
    confidence text not null default 'unknown',
    notes text,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_transactions_property_id on public.transactions (property_id);

create table if not exists public.permits (
    id uuid primary key default gen_random_uuid(),
    property_id uuid not null references public.properties(id) on delete cascade,
    analysis_id uuid references public.analyses(id) on delete set null,
    application_date date,
    decision_date date,
    permit_type text,
    description text,
    status text not null default 'unknown',
    reference_number text,
    authority text,
    source text,
    source_url text,
    confidence text not null default 'unknown',
    affects_investment_case boolean not null default false,
    investment_relevance text,
    notes text,
    is_active boolean not null default false,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_permits_property_id on public.permits (property_id);
create index if not exists idx_permits_status on public.permits (status);

create table if not exists public.energy_labels (
    id uuid primary key default gen_random_uuid(),
    property_id uuid not null references public.properties(id) on delete cascade,
    label text not null,
    source text,
    raw_value text,
    is_current boolean not null default true,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_energy_labels_property_id on public.energy_labels (property_id);
create unique index if not exists uq_energy_labels_current_per_property on public.energy_labels (property_id) where is_current = true;

create table if not exists public.property_enrichments (
    id uuid primary key default gen_random_uuid(),
    property_id uuid not null references public.properties(id) on delete cascade,
    enrichment_key text not null,
    value jsonb,
    source text not null,
    retrieval_date timestamptz not null default timezone('utc', now()),
    confidence_score integer not null default 0,
    success boolean not null default true,
    error_message text,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_property_enrichments_property_id on public.property_enrichments (property_id);
create index if not exists idx_property_enrichments_enrichment_key on public.property_enrichments (enrichment_key);
create index if not exists idx_property_enrichments_created_at on public.property_enrichments (created_at desc);

create table if not exists public.property_enrichment_groups (
    id uuid primary key default gen_random_uuid(),
    property_id uuid not null references public.properties(id) on delete cascade unique,
    status text not null default 'pending',
    started_at timestamptz,
    completed_at timestamptz,
    source text,
    warning_count integer not null default 0,
    error_count integer not null default 0,
    summary jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_property_enrichment_groups_property_id on public.property_enrichment_groups (property_id);
create index if not exists idx_property_enrichment_groups_status on public.property_enrichment_groups (status);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = timezone('utc', now());
    return new;
end;
$$;

drop trigger if exists trg_properties_set_updated_at on public.properties;
create trigger trg_properties_set_updated_at
before update on public.properties
for each row execute function public.set_updated_at();

alter table public.properties enable row level security;
alter table public.analyses enable row level security;
alter table public.transactions enable row level security;
alter table public.permits enable row level security;
alter table public.energy_labels enable row level security;
alter table public.property_enrichments enable row level security;
alter table public.property_enrichment_groups enable row level security;
