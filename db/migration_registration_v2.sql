-- Migration: registration_v2
-- Adds parent_object_id (MOD-kobling til registrert sett),
-- weight_kg (BULK-vekt), part_color_id + part_color_name (løse deler).
-- Kjøres i Supabase SQL Editor.

ALTER TABLE objects
  ADD COLUMN IF NOT EXISTS parent_object_id UUID REFERENCES objects(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS weight_kg        NUMERIC,
  ADD COLUMN IF NOT EXISTS part_color_id    INTEGER,
  ADD COLUMN IF NOT EXISTS part_color_name  TEXT;

-- Index for MOD-oppslag (finn alle MODs av et gitt sett)
CREATE INDEX IF NOT EXISTS idx_objects_parent_object_id
  ON objects(parent_object_id)
  WHERE parent_object_id IS NOT NULL;

-- RLS: parent_object_id arver ingen ekstra policies —
-- brukeren har allerede tilgang via user_id på objektet selv.
