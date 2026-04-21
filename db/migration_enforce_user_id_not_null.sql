-- ============================================================
-- BrickHaus – Enforce user_id NOT NULL on top-level tables
-- Applied: 2026-04-21
-- Pre-check: 0 rows with NULL user_id in all three tables
--   (objects: 585 rows, locations: 6 rows, tags: 0 rows)
-- ============================================================

ALTER TABLE public.objects   ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.locations ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE public.tags      ALTER COLUMN user_id SET NOT NULL;
