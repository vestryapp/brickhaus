-- ============================================================
-- BrickHaus — wear_level enum + column
-- Run against Supabase SQL editor
-- Date: 2026-04-10
--
-- Adds a per-unit wear/condition grading on top of the existing
-- condition_type (which only captures SEALED/OPENED/BUILT/USED/INCOMPLETE).
-- wear_level is collector-grade slitasjenivå and is mainly relevant
-- for OPENED/BUILT/USED/INCOMPLETE units. May be NULL for SEALED.
--
-- Scale (BL-inspired):
--   MINT       — som ny, ingen synlig bruk
--   NEAR_MINT  — minimal slitasje, knapt synlig
--   VERY_GOOD  — lett brukt, små skrammer
--   GOOD       — normalt brukt, tydelige tegn
--   FAIR       — betydelig slitasje, men intakt
-- ============================================================

-- 1. Create the enum (idempotent guard)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'wear_level') THEN
    CREATE TYPE wear_level AS ENUM ('MINT', 'NEAR_MINT', 'VERY_GOOD', 'GOOD', 'FAIR');
  END IF;
END
$$;

-- 2. Add the column (idempotent)
ALTER TABLE objects ADD COLUMN IF NOT EXISTS wear_level wear_level;

COMMENT ON COLUMN objects.wear_level IS
  'Per-unit slitasjegrad. NULL for sealed/ubrukt. MINT/NEAR_MINT/VERY_GOOD/GOOD/FAIR.';
