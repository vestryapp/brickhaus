"""
BrickLink price sync — runs as a Railway cron job (monthly).

Fetches the latest average sold price from BrickLink for all SET objects
that have a set_number, and updates estimated_value_bl in Supabase.

Environment variables required (same as the main app):
  SUPABASE_URL, SUPABASE_SERVICE_KEY,
  BRICKLINK_CONSUMER_KEY, BRICKLINK_CONSUMER_SECRET,
  BRICKLINK_TOKEN, BRICKLINK_TOKEN_SECRET

Run locally:  python3 sync/bl_price_sync.py
"""

import os
import json
import time
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests_oauthlib import OAuth1

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL   = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY   = os.environ["SUPABASE_SERVICE_KEY"]
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


def fetch_all_sets():
    rows, offset, limit = [], 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/objects",
            headers={**SB_HEADERS, "Range": f"{offset}-{offset+limit-1}"},
            params={"select": "ownership_id,set_number,condition",
                    "object_type": "eq.SET",
                    "set_number": "not.is.null"},
        )
        chunk = r.json()
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
    return rows


RB_HEADERS = {"Authorization": f"key {os.environ.get('REBRICKABLE_API_KEY', '')}"}

def _fetch(item_type: str, item_id: str, condition: str, guide_type: str) -> float | None:
    new_or_used = "N" if condition == "SEALED" else "U"
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}/price",
            auth=BL_AUTH,
            params={"guide_type": guide_type, "new_or_used": new_or_used,
                    "currency_code": "NOK", "region": "europe"},
            timeout=10,
        )
        if not r.ok:
            return None
        data = r.json().get("data", {})
        val = data.get("qty_avg_price") or data.get("avg_price")
        return float(val) if val and float(val) > 0 else None
    except Exception:
        return None

def _rb_bl_minifig_id(set_number: str) -> str | None:
    try:
        r = requests.get(f"https://rebrickable.com/api/v3/lego/sets/{set_number}/",
                         headers=RB_HEADERS, timeout=8)
        if not r.ok:
            return None
        for ext in r.json().get("external_ids", {}).get("BrickLink", []):
            if str(ext).startswith("col"):
                return str(ext)
        return None
    except Exception:
        return None

def bl_get_price(set_number: str, condition: str) -> float | None:
    base, _, suffix = set_number.partition("-")
    is_cmf = suffix.isdigit() and int(suffix) > 1
    if is_cmf:
        fig_id = _rb_bl_minifig_id(set_number)
        if fig_id:
            price = (_fetch("MINIFIG", fig_id, condition, "sold") or
                     _fetch("MINIFIG", fig_id, condition, "stock"))
            if price:
                return price
    return (_fetch("SET", base, condition, "sold") or
            _fetch("SET", base, condition, "stock"))


def update_price(ownership_id: str, price: float):
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/objects",
        headers={**SB_HEADERS, "Prefer": "return=minimal"},
        params={"ownership_id": f"eq.{ownership_id}"},
        data=json.dumps({"estimated_value_bl": price}),
    )


if __name__ == "__main__":
    sets = fetch_all_sets()
    print(f"Synkroniserer priser for {len(sets)} sett ...")

    updated = skipped = errors = 0
    for i, obj in enumerate(sets):
        price = bl_get_price(obj["set_number"], obj.get("condition", "USED"))
        if price:
            update_price(obj["ownership_id"], price)
            updated += 1
            print(f"  [{i+1}/{len(sets)}] {obj['ownership_id']} {obj['set_number']}: {price:.0f} kr")
        else:
            skipped += 1
            print(f"  [{i+1}/{len(sets)}] {obj['ownership_id']} {obj['set_number']}: ikke funnet")

        # BrickLink rate limit: stay well under 5000/dag
        time.sleep(0.5)

    print(f"\nFerdig. Oppdatert: {updated}, ikke funnet: {skipped}, feil: {errors}")
