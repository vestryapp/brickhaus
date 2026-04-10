-- ============================================================
-- BrickHaus — image_type enum fix
-- Run against Supabase SQL editor
-- Date: 2026-04-10
--
-- Context:
-- The image_type enum was originally created with lowercase values
-- ('reference', 'documentation'), but all app code uses uppercase
-- ('REFERENCE', 'DOCUMENTATION'). This caused all INSERTs to fail
-- with 400 Bad Request, meaning documentation image uploads never
-- actually worked.
--
-- This migration adds the uppercase values alongside the existing
-- lowercase ones and migrates any existing rows. The lowercase
-- values remain (Postgres cannot easily remove enum values) but
-- are no longer used by the application.
-- ============================================================

-- 1. Add uppercase enum values
ALTER TYPE image_type ADD VALUE IF NOT EXISTS 'REFERENCE';
ALTER TYPE image_type ADD VALUE IF NOT EXISTS 'DOCUMENTATION';

-- 2. Migrate any existing rows to uppercase
-- (Should be 0 rows since inserts have been failing, but safe to run anyway.)
UPDATE images SET image_type = 'REFERENCE'     WHERE image_type = 'reference';
UPDATE images SET image_type = 'DOCUMENTATION' WHERE image_type = 'documentation';

-- 3. Verify: this should return both lowercase and uppercase values
-- SELECT enum_range(NULL::image_type);
