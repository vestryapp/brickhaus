-- ============================================================
-- BrickHaus – Supabase Schema
-- Version: 0.1  |  Phase 0
-- ============================================================

-- Enable UUID generation
create extension if not exists "pgcrypto";


-- ============================================================
-- ENUMS
-- ============================================================

create type object_type as enum (
  'SET', 'MINIFIG', 'PART', 'BULK_CONTAINER', 'MOC', 'MOD'
);

create type condition_type as enum (
  'SEALED', 'OPENED', 'USED', 'INCOMPLETE'
);

create type quality_level as enum (
  'BASIC', 'DOCUMENTED', 'VERIFIED'
);

create type image_type as enum (
  'reference', 'documentation', 'REFERENCE', 'DOCUMENTATION'
);
-- NOTE: App code uses uppercase. Lowercase kept for backwards-compat only.
-- See db/migration_image_type_uppercase.sql.

create type status_type as enum (
  'OWNED', 'SOLD', 'LOANED', 'WANTED'
);


-- ============================================================
-- LOCATIONS
-- Hierarchical: Rom > Møbel/hylle > Boks/skuff
-- ============================================================

create table locations (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  parent_id   uuid references locations(id) on delete set null,
  created_at  timestamptz not null default now()
);

comment on table locations is 'Hierarchical storage locations: room > furniture > container';


-- ============================================================
-- OBJECTS
-- Central table — one row per physical unit
-- ============================================================

create table objects (
  id                    uuid primary key default gen_random_uuid(),
  ownership_id          text unique not null,          -- LG-000001 format
  object_type           object_type not null default 'SET',
  set_number            text,                          -- e.g. '75192'
  bl_item_no            text,                          -- BrickLink item number
  rebrickable_id        text,
  lego_element_id       text,
  name                  text,
  theme                 text,
  subtheme              text,
  year                  integer,
  entity                text,                          -- Polybag, GWP, etc.
  volume                integer,                       -- antall i pakken (f.eks. CMF-pose)

  -- Condition & status
  condition             condition_type not null default 'OPENED',
  seal_status           text,
  box_condition         text,
  set_condition         text,
  parts_condition       text,
  is_built              boolean not null default false,
  completeness_level    text,
  sorting_level         text,
  spares_present        boolean,
  spares_location       text,
  status                status_type not null default 'OWNED',

  -- Parts (only for PART type)
  part_category         text,
  part_color            text,
  part_set_belonging    text,

  -- Location
  location_id           uuid references locations(id) on delete set null,
  sub_location          text,

  -- Manual / instructions
  manual_present        boolean,
  manual_condition      text,
  manual_location       text,

  -- Purchase
  purchase_date         date,
  purchase_source       text,
  order_reference       text,
  purchase_price        numeric(12,2),
  purchase_currency     text default 'NOK',
  shipping_cost         numeric(12,2),
  import_tax            numeric(12,2),
  total_cost_nok        numeric(12,2),

  -- Valuation (latest snapshot — full history in valuations table)
  estimated_value_bl    numeric(12,2),
  estimated_value_manual numeric(12,2),
  valuation_date        date,

  -- Insurance
  insured               boolean default false,
  insurance_value_nok   numeric(12,2),

  -- Sale
  sold_date             date,
  sold_price_nok        numeric(12,2),

  -- Quality level (computed, but stored for performance)
  quality_level         quality_level not null default 'BASIC',

  -- Grouping (e.g. 16 identical CMF bags)
  set_group_id          uuid,

  -- Meta
  image_filename        text,                          -- legacy Excel field
  notes                 text,
  registered_at         date,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now()
);

comment on table objects is 'One row per physical Lego unit. object_type drives which fields are relevant.';
comment on column objects.ownership_id is 'Human-readable unique ID from Excel (LG-000001 format). Preserved for traceability.';
comment on column objects.quality_level is 'BASIC: metadata only. DOCUMENTED: has documentation image. VERIFIED: documented + completeness confirmed.';
comment on column objects.set_group_id is 'Groups identical physical units (e.g. 16 CMF bags of same set). UUID shared across the group.';


-- ============================================================
-- IMAGES
-- ============================================================

create table images (
  id            uuid primary key default gen_random_uuid(),
  object_id     uuid not null references objects(id) on delete cascade,
  image_type    image_type not null,
  storage_path  text not null,        -- Supabase Storage path
  caption       text,
  taken_at      timestamptz,
  created_at    timestamptz not null default now()
);

comment on column images.image_type is 'reference: shared/catalog image. documentation: per physical unit, used for insurance purposes.';


-- ============================================================
-- VALUATIONS
-- Full price history per object
-- ============================================================

create table valuations (
  id                uuid primary key default gen_random_uuid(),
  object_id         uuid not null references objects(id) on delete cascade,
  source            text not null,              -- 'bricklink_new', 'bricklink_used', 'manual'
  price_nok         numeric(12,2) not null,
  price_original    numeric(12,2),
  currency_original text,
  fetched_at        timestamptz not null default now()
);


-- ============================================================
-- MISSING PARTS
-- Parts known to be missing from a specific object
-- ============================================================

create table missing_parts (
  id          uuid primary key default gen_random_uuid(),
  object_id   uuid not null references objects(id) on delete cascade,
  bl_part_id  text not null,
  color_id    integer,
  color_name  text,
  quantity    integer not null default 1,
  notes       text,
  created_at  timestamptz not null default now()
);


-- ============================================================
-- TAGS
-- ============================================================

create table tags (
  id          uuid primary key default gen_random_uuid(),
  name        text unique not null,
  created_at  timestamptz not null default now()
);

create table object_tags (
  object_id   uuid not null references objects(id) on delete cascade,
  tag_id      uuid not null references tags(id) on delete cascade,
  primary key (object_id, tag_id)
);


-- ============================================================
-- UPDATED_AT TRIGGER
-- ============================================================

create or replace function set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger objects_updated_at
  before update on objects
  for each row execute function set_updated_at();


-- ============================================================
-- INDEXES
-- ============================================================

create index idx_objects_object_type    on objects(object_type);
create index idx_objects_theme          on objects(theme);
create index idx_objects_status         on objects(status);
create index idx_objects_quality_level  on objects(quality_level);
create index idx_objects_set_number     on objects(set_number);
create index idx_objects_location_id    on objects(location_id);
create index idx_objects_set_group_id   on objects(set_group_id);
create index idx_images_object_id       on images(object_id);
create index idx_valuations_object_id   on valuations(object_id);
create index idx_missing_parts_object   on missing_parts(object_id);
