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

    print(f"    → raw minifig entry keys: {list(figs[0].keys())}")
    print(f"    → raw minifig entry: {json.dumps(figs[0], indent=6)}")

    fig_num = figs[0].get("fig_num") or figs[0].get("set_num")
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
print("BRICKLINK DIRECT col* LOOKUP — do IDs exist and have price data?")
print("=" * 70)

def bl_minifig_item(col_id):
    """Check if a BL MINIFIG item exists and has a name."""
    auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
    r = requests.get(
        f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{col_id}",
        auth=auth, timeout=10,
    )
    if r.ok:
        d = r.json().get("data", {})
        return d.get("name"), d.get("category_id")
    return f"HTTP {r.status_code}", None

# Test Series 15 (71011) — does col15-X pattern work?
# Print full raw response for one item to see what BL actually returns
auth_test = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
r_test = requests.get("https://api.bricklink.com/api/store/v1/items/MINIFIG/col15-1",
                      auth=auth_test, timeout=10)
print(f"\nRaw response for MINIFIG/col15-1:")
print(f"  HTTP {r_test.status_code}")
print(f"  Body: {r_test.text[:500]}")

print("\nSeries 15 (71011) — col15-1 through col15-5:")
for i in range(1, 6):
    name, cat = bl_minifig_item(f"col15-{i}")
    print(f"  col15-{i}: {name}  category={cat}")

# Test Series 1 (8683) — sequential col001-col005
print("\nSeries 1 (8683) — col001 through col005:")
for i in range(1, 6):
    name, cat = bl_minifig_item(f"col{i:03d}")
    print(f"  col{i:03d}: {name}  category={cat}")

# And check price for col15-16 specifically (Queen candidate)
print("\nPrice check for col15-16 (Queen candidate):")
qa, avg, qty = bl_price("MINIFIG", "col15-16", "U", "sold")
print(f"  sold:  qty_avg={qa}  avg={avg}  total_qty={qty}")
qa, avg, qty = bl_price("MINIFIG", "col15-16", "U", "stock")
print(f"  stock: qty_avg={qa}  avg={avg}  total_qty={qty}")

print()
print("=" * 70)
print("BRICKLINK SUBSETS — CMF series display box → individual figures")
print("=" * 70)

def bl_subsets(item_type, item_id):
    auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
    r = requests.get(
        f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}/subsets",
        auth=auth, timeout=10,
    )
    if r.ok:
        return r.json().get("data", [])
    return f"HTTP {r.status_code}: {r.text[:200]}"

for series_set in ["8683-1", "71011-1"]:
    print(f"\nSubsets of SET/{series_set}:")
    result = bl_subsets("SET", series_set)
    if isinstance(result, str):
        print(f"  ERROR: {result}")
    else:
        print(f"  {len(result)} subset entries")
        for entry in result[:5]:   # first 5 only
            for item in entry.get("entries", []):
                no   = item.get("item", {}).get("no")
                typ  = item.get("item", {}).get("type")
                name = item.get("item", {}).get("name", "?")
                qty  = item.get("quantity")
                print(f"    {typ}/{no}  qty={qty}  {name}")

print()
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
