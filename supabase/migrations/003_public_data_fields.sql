alter table public.properties

    add column if not exists bag_building_year integer,

    add column if not exists bag_floor_area numeric,

    add column if not exists bag_usage_purpose text,

    add column if not exists bag_status text,

    add column if not exists bag_object_id text,

    add column if not exists woz_value numeric,

    add column if not exists woz_reference_date date,

    add column if not exists woz_value_per_m2 numeric,

    add column if not exists price_vs_woz_percentage numeric,

    add column if not exists public_data_source text,

    add column if not exists public_data_confidence text,

    add column if not exists public_data_updated_at timestamptz;alter table public.properties

    add column if not exists bag_building_year integer,

    add column if not exists bag_floor_area numeric,

    add column if not exists bag_usage_purpose text,

    add column if not exists bag_status text,

    add column if not exists bag_object_id text,

    add column if not exists woz_value numeric,

    add column if not exists woz_reference_date date,

    add column if not exists woz_value_per_m2 numeric,

    add column if not exists price_vs_woz_percentage numeric,

    add column if not exists public_data_source text,

    add column if not exists public_data_confidence text,

    add column if not exists public_data_updated_at timestamptz;
    