-- ============================================================
-- BrickHaus – Storage bucket for documentation images
-- Run this in Supabase SQL Editor
-- ============================================================

-- Create the storage bucket (public = anyone can read URLs)
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES (
    'object-images',
    'object-images',
    true,
    10485760,   -- 10 MB max per file
    ARRAY['image/jpeg', 'image/png', 'image/webp']
)
ON CONFLICT (id) DO NOTHING;

-- Service role (Railway/server) can upload and delete
CREATE POLICY "service role can manage images"
ON storage.objects FOR ALL
TO service_role
USING (bucket_id = 'object-images')
WITH CHECK (bucket_id = 'object-images');

-- Public read access (no auth needed to view images)
CREATE POLICY "public can read images"
ON storage.objects FOR SELECT
TO public
USING (bucket_id = 'object-images');
