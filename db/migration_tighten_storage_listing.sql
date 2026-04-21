-- ============================================================
-- BrickHaus – Tighten object-images bucket listing
-- Applied: 2026-04-21
-- Problem: "public can read images" allowed anyone to call
--   the Storage API and enumerate all filenames in the bucket.
--   The app never lists the bucket – it only builds direct URLs.
--   The bucket stays public so all existing URLs keep working.
-- Fix: drop the broad public SELECT policy.
--   Replace with an authenticated-user policy scoped to their
--   own objects (path format: {object_uuid}/{filename}).
-- ============================================================

-- 1. Remove the broad public listing policy
DROP POLICY IF EXISTS "public can read images" ON storage.objects;

-- 2. Allow authenticated users to SELECT only their own images
--    (needed for Storage API calls from the app when using the
--     anon/user token; service_role is already unrestricted above)
CREATE POLICY "authenticated users can read own images"
ON storage.objects FOR SELECT
TO authenticated
USING (
    bucket_id = 'object-images'
    AND EXISTS (
        SELECT 1 FROM public.objects o
        WHERE o.user_id = auth.uid()
          AND storage.objects.name LIKE (o.id::text || '/%')
    )
);
