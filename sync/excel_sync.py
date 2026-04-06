"""
BrickHaus – Excel → Supabase sync script
Phase 0: One-way sync from lego_database_basis.xlsx to Supabase REST API.

Run:  python sync/excel_sync.py
Re-runnable: uses ownership_id as upsert key — safe to run multiple times.
Remove after cutover.
"""

import os
import sys
import json
from datetime import datetime, date
from pathlib import Path

import requests
import openpyxl
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
EXCEL_PATH   = os.environ.get("EXCEL_PATH", "../data/lego_database_basis.xlsx")

HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=merge-duplicates,return=minimal",
}


def rest(table: str, rows: list[dict]) -> None:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    r = requests.post(url, headers=HEADERS, data=json.dumps(rows, default=str))
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Supabase error {r.status_code}: {r.text[:300]}")


# ── Value mappers ─────────────────────────────────────────────────────────────

OBJECT_TYPE_MAP = {
    "set":            "SET",
    "minifigure":     "MINIFIG",
    "parts":          "PART",
    "sticker sheet":  "PART",
    "bulk":           "BULK_CONTAINER",
    "moc":            "MOC",
    "mod":            "MOD",
}

CONDITION_MAP = {
    "sealed":     "SEALED",
    "opened":     "OPENED",
    "used":       "USED",
    "incomplete": "INCOMPLETE",
}

def map_object_type(raw) -> str:
    if not raw:
        return "SET"
    return OBJECT_TYPE_MAP.get(str(raw).strip().lower(), "SET")

def map_condition(raw) -> str:
    if not raw:
        return "OPENED"
    return CONDITION_MAP.get(str(raw).strip().lower(), "OPENED")

def to_bool(raw):
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("yes", "ja", "true", "1", "x"):
        return True
    if s in ("no", "nei", "false", "0", "na", ""):
        return False
    return None

def to_date(raw):
    if raw is None:
        return None
    if isinstance(raw, (datetime, date)):
        return raw.strftime("%Y-%m-%d")
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").strftime("%Y-%m-%d")
    except Exception:
        return None

def to_numeric(raw):
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

def clean(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower() in ("na", "n/a", "none", ""):
        return None
    return s


# ── Location cache ────────────────────────────────────────────────────────────

_location_cache: dict[str, str] = {}

def get_or_create_location(room) -> str | None:
    if not room:
        return None
    key = str(room).strip()
    if key in _location_cache:
        return _location_cache[key]

    # Check existing
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/locations",
        headers={**HEADERS, "Prefer": ""},
        params={"name": f"eq.{key}", "select": "id"},
    )
    data = r.json()
    if data:
        _location_cache[key] = data[0]["id"]
        return _location_cache[key]

    # Create new
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/locations",
        headers={**HEADERS, "Prefer": "return=representation"},
        data=json.dumps([{"name": key}]),
    )
    if r.status_code in (200, 201):
        _location_cache[key] = r.json()[0]["id"]
        return _location_cache[key]

    return None


# ── Column index map ──────────────────────────────────────────────────────────

COL = {
    "ownership_id":        0,
    "set_number":          1,
    "theme":               2,
    "subtheme":            3,
    "name":                4,
    "bl_item_no":          5,
    "year":                6,
    "object_type_raw":     7,
    "volume":              8,
    "condition_raw":       9,
    "seal_status":         10,
    "box_condition":       11,
    "is_built_raw":        12,
    "set_condition":       13,
    "completeness_level":  14,
    "parts_condition":     15,
    "sorting_level":       16,
    "entity":              17,
    "part_category":       18,
    "part_color":          19,
    "part_set_belonging":  20,
    "status_raw":          21,
    "location_raw":        22,
    "sub_location":        23,
    "spares_present_raw":  24,
    "spares_location":     25,
    "manual_present_raw":  26,
    "manual_condition":    27,
    "manual_location":     28,
    "image_filename":      29,
    "notes":               30,
    "rebrickable_id":      31,
    "lego_element_id":     32,
    "registered_at_raw":   33,
    "updated_at_raw":      34,
    "purchase_date_raw":   35,
    "purchase_source":     36,
    "order_reference":     37,
    "purchase_price":      38,
    "purchase_currency":   39,
    "shipping_cost":       40,
    "import_tax":          41,
    "total_cost_nok":      42,
    "estimated_value_bl":  43,
    "estimated_value_be":  44,   # BrickEconomy — not used, but preserved
    "estimated_value_manual": 45,
    "valuation_date_raw":  46,
    "insured_raw":         47,
    "insurance_value_nok": 48,
    "sold_date_raw":       49,
    "sold_price_nok":      50,
}

def g(row, field):
    """Get value from row by field name."""
    return row[COL[field]]

def map_row(row: tuple) -> dict:
    location_id = get_or_create_location(g(row, "location_raw"))
    return {
        "ownership_id":           g(row, "ownership_id"),
        "object_type":            map_object_type(g(row, "object_type_raw")),
        "set_number":             clean(g(row, "set_number")),
        "bl_item_no":             clean(g(row, "bl_item_no")),
        "rebrickable_id":         clean(g(row, "rebrickable_id")),
        "lego_element_id":        clean(g(row, "lego_element_id")),
        "name":                   clean(g(row, "name")),
        "theme":                  clean(g(row, "theme")),
        "subtheme":               clean(g(row, "subtheme")),
        "year":                   int(g(row, "year")) if g(row, "year") else None,
        "entity":                 clean(g(row, "entity")),
        "volume":                 int(g(row, "volume")) if g(row, "volume") else None,
        "condition":              map_condition(g(row, "condition_raw")),
        "seal_status":            clean(g(row, "seal_status")),
        "box_condition":          clean(g(row, "box_condition")),
        "set_condition":          clean(g(row, "set_condition")),
        "parts_condition":        clean(g(row, "parts_condition")),
        "is_built":               to_bool(g(row, "is_built_raw")) or False,
        "completeness_level":     clean(g(row, "completeness_level")),
        "sorting_level":          clean(g(row, "sorting_level")),
        "spares_present":         to_bool(g(row, "spares_present_raw")),
        "spares_location":        clean(g(row, "spares_location")),
        "status":                 "OWNED",
        "part_category":          clean(g(row, "part_category")),
        "part_color":             clean(g(row, "part_color")),
        "part_set_belonging":     clean(g(row, "part_set_belonging")),
        "location_id":            location_id,
        "sub_location":           clean(g(row, "sub_location")),
        "manual_present":         to_bool(g(row, "manual_present_raw")),
        "manual_condition":       clean(g(row, "manual_condition")),
        "manual_location":        clean(g(row, "manual_location")),
        "purchase_date":          to_date(g(row, "purchase_date_raw")),
        "purchase_source":        clean(g(row, "purchase_source")),
        "order_reference":        clean(g(row, "order_reference")),
        "purchase_price":         to_numeric(g(row, "purchase_price")),
        "purchase_currency":      clean(g(row, "purchase_currency")) or "NOK",
        "shipping_cost":          to_numeric(g(row, "shipping_cost")),
        "import_tax":             to_numeric(g(row, "import_tax")),
        "total_cost_nok":         to_numeric(g(row, "total_cost_nok")),
        "estimated_value_bl":     to_numeric(g(row, "estimated_value_bl")),
        "estimated_value_manual": to_numeric(g(row, "estimated_value_manual")),
        "valuation_date":         to_date(g(row, "valuation_date_raw")),
        "insured":                to_bool(g(row, "insured_raw")) or False,
        "insurance_value_nok":    to_numeric(g(row, "insurance_value_nok")),
        "sold_date":              to_date(g(row, "sold_date_raw")),
        "sold_price_nok":         to_numeric(g(row, "sold_price_nok")),
        "image_filename":         clean(g(row, "image_filename")),
        "notes":                  clean(g(row, "notes")),
        "registered_at":          to_date(g(row, "registered_at_raw")),
        "quality_level":          "BASIC",
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    path = Path(EXCEL_PATH).resolve()
    if not path.exists():
        # Try relative to script location
        path = (Path(__file__).parent.parent / "data" / "lego_database_basis.xlsx").resolve()
    if not path.exists():
        print(f"ERROR: Excel file not found. Set EXCEL_PATH in .env")
        sys.exit(1)

    print(f"Reading {path.name} ...")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Lego_Ownership"]

    rows = [r for r in ws.iter_rows(min_row=2, values_only=True) if r[0] is not None]
    print(f"Found {len(rows)} rows.")

    batch, errors = [], []
    for i, raw in enumerate(rows, start=2):
        try:
            batch.append(map_row(raw))
        except Exception as e:
            errors.append((i, raw[0], str(e)))

    if errors:
        print(f"\nWARNING: {len(errors)} rows skipped:")
        for row_num, oid, msg in errors:
            print(f"  Row {row_num} ({oid}): {msg}")

    BATCH_SIZE = 50
    inserted = 0
    for start in range(0, len(batch), BATCH_SIZE):
        chunk = batch[start:start + BATCH_SIZE]
        rest("objects", chunk)
        inserted += len(chunk)
        print(f"  Upserted {inserted}/{len(batch)} ...", end="\r")

    print(f"\nDone. {inserted} objects synced, {len(errors)} skipped.")


if __name__ == "__main__":
    main()
