-- ============================================================
-- Migration: Add name_bl field to objects
-- BrickLink name = authoritative source (LEGO-owned)
-- Run in Supabase SQL Editor
-- ============================================================

-- Add BrickLink name field (full official name)
ALTER TABLE objects
  ADD COLUMN IF NOT EXISTS name_bl text;

COMMENT ON COLUMN objects.name_bl IS
  'Official name from BrickLink catalog (e.g. "Queen, Series 15 (Complete Set with Stand and Accessories)"). Displayed trimmed in UI; Rebrickable name kept in name field as fallback.';
