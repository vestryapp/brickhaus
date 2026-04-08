-- ============================================================
-- BrickHaus – MOC/MOD fields
-- Run this in Supabase SQL Editor
-- ============================================================

-- Part count (for MOC/MOD — and optionally other types)
ALTER TABLE objects ADD COLUMN IF NOT EXISTS num_parts INTEGER;

-- For MOD: which set this is based on (e.g. '75192-1')
ALTER TABLE objects ADD COLUMN IF NOT EXISTS moc_base_set TEXT;

-- Instructions link (e.g. Rebrickable MOC page, YouTube, etc.)
ALTER TABLE objects ADD COLUMN IF NOT EXISTS instructions_url TEXT;

-- Instructions file uploaded to Supabase Storage
ALTER TABLE objects ADD COLUMN IF NOT EXISTS instructions_storage_path TEXT;

-- Rebrickable MOC ID (e.g. 'MOC-12345')
ALTER TABLE objects ADD COLUMN IF NOT EXISTS rebrickable_moc_id TEXT;
