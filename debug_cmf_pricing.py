"""
Diagnostic script: check Rebrickable → BrickLink ID chain for CMF figures.
Uses same API parameters as the main app.

Run from brickhaus/ folder:
  python3 debug_cmf_pricing.py
"""

import os, json, requests
from pathlib import Path
from dotenv import load_dotenv
from requests_oauthlib import OAuth1

load_dotenv(Path(__file__).parent / ".env")

RB_KEY = os.environ.get("REBRICKABLE_API_KEY", "")
BL_CONSUMER_KEY    = os.environ.get("BRICKLINK_CONSUMER_KEY", "")
BL_CONSUMER_SECRET = os.environ.get("BRICKLINK_CONSUMER_SECRET", "")
BL_TOKEN           = os.environ.get("BRICKLINK_TOKEN", "")
BL_TOKEN_SECRET    = os.environ.get("BRICKLINK_TOKEN_SECRET", "")

RB_HEADERS = {"Authorization": f"key {RB_KEY}"}

def bl_price(item_type, item_id, condition="U", guide_type="sold"):
    auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
    r = requests.get(
        f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}/price",
        params={"new_or_used": condition, "guide_type": guide_type,
                "currency_code": "NOK", "region": "europe"},  # matches main app
        auth=auth, timeout=10,
    )
    if r.ok:
        d = r.json().get("data", {})
        return d.get("qty_avg_price"), d.get("avg_price"), d.get("total_quantity")
    return f"HTTP {r.status_code}", None, None

def rb_get_bl_minifig_id(set_number):
    """Two-step lookup matching the updated main app logic."""
    # Step 1: set external_ids
    r = requests.get(f"https://rebrickable.com/api/v3/lego/sets/{set_number}/",
                     headers=RB_HEADERS, timeout=8)
    if not r.ok:
        return None, f"Set lookup failed: HTTP {r.status_code}"
    set_data = r.json()
    for ext in set_data.get("external_ids", {}).get("BrickLink", []):
        if str(ext).startswith("col"):
            return str(ext), "via set external_ids"

    # Step 2: set → minifig → minifig external_ids
    r2 = requests.get(f"https://rebrickable.com/api/v3/lego/sets/{set_number}/minifigs/",
                      headers=RB_HEADERS, timeout=8)
    if not r2.ok:
        return None, f"Minifig list failed: HTTP {r2.status_code}"
    figs = r2.json().get("results", [])
    if not figs:
        return None, "No minifigs in set"
    if len(figs) > 1:
        return None, f"Ambiguous: {len(figs)} minifigs in set"

    fig_num = figs[0].get("fig_num")
    print(f"    → found minifig {fig_num} ({figs[0].get('name','?')})")

    r3 = requests.get(f"https://rebrickable.com/api/v3/lego/minifigs/{fig_num}/",
                      headers=RB_HEADERS, timeout=8)
    if not r3.ok:
        return None, f"Minifig detail failed: HTTP {r3.status_code}"
    fig_data = r3.json()
    bl_ids = fig_data.get("external_ids", {}).get("BrickLink", [])
    print(f"    → minifig BrickLink IDs: {bl_ids}")
    for ext in bl_ids:
        if str(ext).startswith("col"):
            return str(ext), "via minifig external_ids"
    return None, f"No col* ID in minifig external_ids: {bl_ids}"

# ── Test ──────────────────────────────────────────────────────────────────────
TEST_SETS = ["71011-16", "71011-1", "71029-1", "8683-1"]

print("=" * 70)
print("TWO-STEP REBRICKABLE → BRICKLINK ID LOOKUP")
print("=" * 70)
for sn in TEST_SETS:
    print(f"\n{sn}:")
    col_id, note = rb_get_bl_minifig_id(sn)
    print(f"  Result: {col_id}  ({note})")
    if col_id:
        qa, avg, qty = bl_price("MINIFIG", col_id, "U", "sold")
        print(f"  BL MINIFIG/{col_id} sold:  qty_avg={qa}  avg={avg}  total_qty={qty}")
        qa, avg, qty = bl_price("MINIFIG", col_id, "U", "stock")
        print(f"  BL MINIFIG/{col_id} stock: qty_avg={qa}  avg={avg}  total_qty={qty}")
