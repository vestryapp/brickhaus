-- Migration: Add BUILT to condition_type enum
-- Run once in Supabase SQL Editor.
-- Safe to run multiple times (IF NOT EXISTS syntax not available for enum values,
-- but Postgres will error harmlessly if the value already exists — just ignore it).

ALTER TYPE condition_type ADD VALUE IF NOT EXISTS 'BUILT';

-- Optional: update any existing rows where is_built = true and condition = 'OPENED'
-- to use the new BUILT value instead.
UPDATE objects
SET condition = 'BUILT'
WHERE condition = 'OPENED'
  AND is_built = true;
