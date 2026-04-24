-- Migration: inventory_parts (Fase 2 – deleliste)
-- Oppretter inventory_parts-tabellen for å lagre forventede og tilstedeværende
-- deler per SET-objekt. Populeres fra Rebrickable parts-API.
-- Kjøres i Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS inventory_parts (
  id            UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
  object_id     UUID    NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  user_id       UUID    NOT NULL REFERENCES auth.users(id),
  part_num      TEXT    NOT NULL,
  color_id      INTEGER NOT NULL,
  color_name    TEXT    NOT NULL,
  qty_expected  INTEGER NOT NULL DEFAULT 0,
  qty_present   INTEGER NOT NULL DEFAULT 0,
  is_spare      BOOLEAN NOT NULL DEFAULT false,
  part_name     TEXT,
  part_img_url  TEXT,
  used_in_mod   BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- Forhindrer duplikater ved re-henting; bevarer qty_present ved ON CONFLICT DO NOTHING
  CONSTRAINT inventory_parts_unique_part UNIQUE (object_id, part_num, color_id)
);

-- Indeks for rask oppslag per objekt (vanligste spørring)
CREATE INDEX IF NOT EXISTS idx_inventory_parts_object_id
  ON inventory_parts(object_id);

-- Oppdater updated_at automatisk ved endring
CREATE TRIGGER set_inventory_parts_updated_at
  BEFORE UPDATE ON inventory_parts
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- RLS: brukere ser og endrer kun egne rader
ALTER TABLE inventory_parts ENABLE ROW LEVEL SECURITY;

CREATE POLICY "inventory_parts: eier leser egne"
  ON inventory_parts FOR SELECT
  USING (user_id = auth.uid());

CREATE POLICY "inventory_parts: eier setter inn egne"
  ON inventory_parts FOR INSERT
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "inventory_parts: eier oppdaterer egne"
  ON inventory_parts FOR UPDATE
  USING (user_id = auth.uid());

CREATE POLICY "inventory_parts: eier sletter egne"
  ON inventory_parts FOR DELETE
  USING (user_id = auth.uid());
