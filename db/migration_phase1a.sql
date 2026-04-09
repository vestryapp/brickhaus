-- ============================================================
-- BrickHaus — Phase 1a Migration
-- Run against Supabase SQL editor
-- Date: 2026-04-09
-- ============================================================

-- 1. Add new object_type enum values
ALTER TYPE object_type ADD VALUE IF NOT EXISTS 'GEAR';
ALTER TYPE object_type ADD VALUE IF NOT EXISTS 'BOOK';
ALTER TYPE object_type ADD VALUE IF NOT EXISTS 'CATALOG';
ALTER TYPE object_type ADD VALUE IF NOT EXISTS 'INSTRUCTION';
ALTER TYPE object_type ADD VALUE IF NOT EXISTS 'ORIGINAL_BOX';
ALTER TYPE object_type ADD VALUE IF NOT EXISTS 'BULK';

-- NOTE: Renaming BULK_CONTAINER → BULK in a Postgres enum is tricky.
-- Strategy: add BULK as new value (above), then migrate existing rows,
-- then optionally drop BULK_CONTAINER later.
-- For now, both values coexist; code uses BULK for new objects.
UPDATE objects SET object_type = 'BULK' WHERE object_type = 'BULK_CONTAINER';

-- 2. Add new columns to objects table
ALTER TABLE objects ADD COLUMN IF NOT EXISTS num_minifigs       integer;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS has_instructions   boolean NOT NULL DEFAULT false;
ALTER TABLE objects ADD COLUMN IF NOT EXISTS has_original_box   boolean NOT NULL DEFAULT false;

-- 3. Verify existing columns are present (these should already exist)
-- num_parts           integer       — already exists
-- is_built            boolean       — already exists
-- status              status_type   — already exists (OWNED/SOLD/LOANED/WANTED)
-- completeness_level  text          — already exists
-- registered_at       date          — already exists
-- created_at          timestamptz   — already exists

-- 4. Add comments for new columns
COMMENT ON COLUMN objects.num_minifigs     IS 'Number of minifigures included (for SET type). Auto-filled from Rebrickable.';
COMMENT ON COLUMN objects.has_instructions IS 'Whether instructions are present. Affects completeness.';
COMMENT ON COLUMN objects.has_original_box IS 'Whether original box is present. Affects value but not completeness.';
