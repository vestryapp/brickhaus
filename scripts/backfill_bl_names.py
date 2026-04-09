"""
One-time backfill: fetch BrickLink official names for all objects.

Must be run from Railway (BL OAuth is IP-locked).
Usage:  python3 scripts/backfill_bl_names.py

This script fetches the official BrickLink catalog name for every SET, MINIFIG,
and GEAR object, and stores it in the name_bl column. It skips objects that
already have a name_bl value.

Prerequisites:
  - Run db/migrate_name_bl.sql in Supabase SQL Editor first
  - Same env vars as bl_price_sync.py
"""

import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests_oauthlib import OAuth1

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BL_AUTH = OAuth1(
    os.environ["BRICKLINK_CONSUMER_KEY"],
    os.environ["BRICKLINK_CONSUMER_SECRET"],
    os.environ["BRICKLINK_TOKEN"],
    os.environ["BRICKLINK_TOKEN_SECRET"],
)

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

RB_HEADERS = {"Authorization": f"key {os.environ.get('REBRICKABLE_API_KEY', '')}"}


# ── CMF series mapping (same as in bl_price_sync.py) ─────────────────────────
_CMF_SERIES_BY_BASE: dict[str, int] = {
    "8683": 1,  "8684": 2,  "8803": 3,  "8804": 4,
    "8805": 5,  "8827": 6,  "8831": 7,  "8833": 8,
    "71000": 9, "71001": 10, "71002": 11, "71004": 12,
    "71007": 13, "71008": 14, "71011": 15, "71013": 16,
    "71018": 17, "71021": 18, "71025": 19, "71027": 20,
    "71029": 21, "71032": 22, "71034": 23, "71037": 24,
    "71038": 25,
}


def _cmf_derived_bl_id(set_number: str) -> str | None:
    base, _, variant = set_number.partition("-")
    if not variant.isdigit():
        return None
    series = _CMF_SERIES_BY_BASE.get(base)
    if not series:
        return None
    var_num = int(variant)
    if series <= 8:
        return f"col{(series - 1) * 16 + var_num:03d}"
    return f"col{series}-{variant}"


def _rb_get_bl_minifig_id(set_number: str) -> str | None:
    """Find BrickLink MINIFIG ID (col*) for a CMF figure via Rebrickable."""
    try:
        r = requests.get(f"https://rebrickable.com/api/v3/lego/sets/{set_number}/",
                         headers=RB_HEADERS, timeout=8)
        if not r.ok:
            return None
        for ext in r.json().get("external_ids", {}).get("BrickLink", []):
            if str(ext).startswith("col"):
                return str(ext)

        r2 = requests.get(f"https://rebrickable.com/api/v3/lego/sets/{set_number}/minifigs/",
                          headers=RB_HEADERS, timeout=8)
        if not r2.ok:
            return None
        figs = r2.json().get("results", [])
        if len(figs) != 1:
            return None
        fig_num = figs[0].get("set_num")
        if not fig_num:
            return None
        r3 = requests.get(f"https://rebrickable.com/api/v3/lego/minifigs/{fig_num}/",
                          headers=RB_HEADERS, timeout=8)
        if not r3.ok:
            return None
        for ext in r3.json().get("external_ids", {}).get("BrickLink", []):
            if str(ext).startswith("col"):
                return str(ext)

        return _cmf_derived_bl_id(set_number)
    except Exception:
        return None


def fetch_bl_name(item_type: str, item_id: str) -> str | None:
    """Fetch official BrickLink catalog name."""
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}",
            auth=BL_AUTH, timeout=8,
        )
        if not r.ok:
            return None
        return r.json().get("data", {}).get("name") or None
    except Exception:
        return None


def resolve_bl_name(set_number: str, object_type: str) -> str | None:
    """Resolve the BrickLink name for an object, trying the right item type."""
    base, _, suffix = set_number.partition("-")
    is_cmf = suffix.isdigit() and int(suffix) > 1

    # MINIFIG / CMF path
    if object_type == "MINIFIG" or is_cmf:
        fig_id = _rb_get_bl_minifig_id(set_number)
        if fig_id:
            return fetch_bl_name("MINIFIG", fig_id)
        return None

    # SET path — try base, then base-1
    for item_id in (base, f"{base}-1"):
        name = fetch_bl_name("SET", item_id)
        if name:
            return name

    # GEAR fallback
    return fetch_bl_name("GEAR", base)


def fetch_objects_without_bl_name():
    """Fetch all objects that have a set_number but no name_bl."""
    rows, limit = [], 1000
    for obj_type in ("SET", "MINIFIG"):
        offset = 0
        while True:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/objects",
                headers={**SB_HEADERS, "Range": f"{offset}-{offset+limit-1}"},
                params={
                    "select": "ownership_id,set_number,object_type",
                    "object_type": f"eq.{obj_type}",
                    "set_number": "not.is.null",
                    "name_bl": "is.null",
                },
            )
            chunk = r.json()
            if not chunk:
                break
            rows.extend(chunk)
            if len(chunk) < limit:
                break
            offset += limit
    return rows


def update_name_bl(ownership_id: str, name_bl: str):
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/objects",
        headers={**SB_HEADERS, "Prefer": "return=minimal"},
        params={"ownership_id": f"eq.{ownership_id}"},
        data=json.dumps({"name_bl": name_bl}),
    )


if __name__ == "__main__":
    objects = fetch_objects_without_bl_name()
    print(f"Henter BrickLink-navn for {len(objects)} objekter uten name_bl ...")

    updated = skipped = 0
    for i, obj in enumerate(objects):
        bl_name = resolve_bl_name(obj["set_number"], obj.get("object_type", "SET"))
        if bl_name:
            update_name_bl(obj["ownership_id"], bl_name)
            updated += 1
            print(f"  [{i+1}/{len(objects)}] {obj['ownership_id']} {obj['set_number']}: {bl_name}")
        else:
            skipped += 1
            print(f"  [{i+1}/{len(objects)}] {obj['ownership_id']} {obj['set_number']}: ikke funnet")

        time.sleep(0.3)  # BrickLink rate limit

    print(f"\nFerdig. Oppdatert: {updated}, ikke funnet: {skipped}")
