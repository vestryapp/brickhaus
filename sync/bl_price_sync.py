"""
BrickLink price sync — runs as a Railway cron job (monthly).

Fetches a quantity-weighted average price (sold + stock combined) from BrickLink
for all SET and MINIFIG objects that have a set_number, and updates
estimated_value_bl in Supabase.

Pricing strategy: weighted average across sold (last 6 months) and stock (current
listings), weighted by unit count. This prevents a single high-priced outlier sale
from skewing the result when few transactions exist.

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

RB_HEADERS = {"Authorization": f"key {os.environ.get('REBRICKABLE_API_KEY', '')}"}


def fetch_all_sets():
    """Fetch all SET and MINIFIG objects that have a set_number."""
    rows, limit = [], 1000
    for obj_type in ("SET", "MINIFIG"):
        offset = 0
        while True:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/objects",
                headers={**SB_HEADERS, "Range": f"{offset}-{offset+limit-1}"},
                params={"select": "ownership_id,set_number,condition,object_type",
                        "object_type": f"eq.{obj_type}",
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


def _fetch_raw(item_type: str, item_id: str, condition: str, guide_type: str) -> dict | None:
    """Single BrickLink price API call. Returns raw data dict or None."""
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
        return r.json().get("data") or None
    except Exception:
        return None


def _weighted_price(sold: dict | None, stock: dict | None) -> float | None:
    """
    Quantity-weighted average across sold (last 6 months) and stock (current listings).
    Prevents single high-priced outlier sales from dominating when few transactions exist.

    Falls back to simple avg_price if BrickLink reports total_quantity=0
    (can happen for items with historical price data but no recent activity).
    """
    def extract(d: dict | None) -> tuple[float, int]:
        if not d:
            return 0.0, 0
        qty = int(d.get("total_quantity") or 0)
        avg = float(d.get("qty_avg_price") or d.get("avg_price") or 0)
        return avg, qty

    sold_avg,  sold_qty  = extract(sold)
    stock_avg, stock_qty = extract(stock)
    total = sold_qty + stock_qty

    if total > 0:
        weighted = (sold_avg * sold_qty + stock_avg * stock_qty) / total
        return round(weighted, 2) if weighted > 0 else None

    # total_quantity=0 but price data exists (older/rare items) – use avg directly
    if sold_avg > 0:
        return round(sold_avg, 2)
    if stock_avg > 0:
        return round(stock_avg, 2)
    return None


def _rb_bl_minifig_id(set_number: str) -> str | None:
    """Ask Rebrickable for the BrickLink MINIFIG id of a CMF variant."""
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


def bl_get_price(set_number: str, condition: str, object_type: str = "SET") -> float | None:
    """
    Fetch BrickLink price (NOK) using a quantity-weighted average of sold + stock data.

    Lookup strategy:
      1. MINIFIG object_type or CMF variant (suffix > 1): look up as MINIFIG via Rebrickable
      2. SET: try BrickLink SET type with base number
      3. GEAR fallback: keychains, accessories and GWP items BL lists as GEAR not SET
    """
    base, _, suffix = set_number.partition("-")
    is_cmf = suffix.isdigit() and int(suffix) > 1

    # ── MINIFIG / CMF path ────────────────────────────────────────────────────
    if object_type == "MINIFIG" or is_cmf:
        fig_id = _rb_bl_minifig_id(set_number)
        if fig_id:
            price = _weighted_price(
                _fetch_raw("MINIFIG", fig_id, condition, "sold"),
                _fetch_raw("MINIFIG", fig_id, condition, "stock"),
            )
            if price:
                return price
        if object_type == "MINIFIG":
            return None  # Don't fall through to SET for explicit MINIFIGs

    # ── SET path ──────────────────────────────────────────────────────────────
    # Try base number first (BrickLink standard), then with "-1" suffix.
    # Some sets (polybags, older sets) are only found with the revision suffix.
    price = _weighted_price(
        _fetch_raw("SET", base, condition, "sold"),
        _fetch_raw("SET", base, condition, "stock"),
    )
    if not price:
        price = _weighted_price(
            _fetch_raw("SET", f"{base}-1", condition, "sold"),
            _fetch_raw("SET", f"{base}-1", condition, "stock"),
        )
    if price:
        return price

    # ── GEAR fallback (keychains, accessories, GWP items) ────────────────────
    return _weighted_price(
        _fetch_raw("GEAR", base, condition, "sold"),
        _fetch_raw("GEAR", base, condition, "stock"),
    )


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

    updated = skipped = 0
    for i, obj in enumerate(sets):
        price = bl_get_price(obj["set_number"], obj.get("condition", "USED"), obj.get("object_type", "SET"))
        if price:
            update_price(obj["ownership_id"], price)
            updated += 1
            print(f"  [{i+1}/{len(sets)}] {obj['ownership_id']} {obj['set_number']}: {price:.0f} kr")
        else:
            skipped += 1
            print(f"  [{i+1}/{len(sets)}] {obj['ownership_id']} {obj['set_number']}: ikke funnet")

        # BrickLink rate limit: stay well under 5000/dag
        time.sleep(0.5)

    print(f"\nFerdig. Oppdatert: {updated}, ikke funnet: {skipped}")
