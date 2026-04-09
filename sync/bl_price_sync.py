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
import html
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
                params={"select": "ownership_id,set_number,bl_item_no,condition,object_type,name",
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


def _fetch_bl_name(item_type: str, item_id: str) -> str | None:
    """Fetch the official BrickLink catalog name for an item."""
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}",
            auth=BL_AUTH, timeout=8,
        )
        if not r.ok:
            return None
        raw = r.json().get("data", {}).get("name") or None
        return html.unescape(raw) if raw else None
    except Exception:
        return None


_USD_TO_NOK = 10.5  # approximate fallback rate

def _fetch_one(item_type: str, item_id: str,
               new_or_used: str, guide_type: str) -> dict | None:
    """Try NOK/Europe, then USD global for a single condition."""
    url = f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}/price"
    try:
        r = requests.get(url, auth=BL_AUTH, params={
            "guide_type": guide_type, "new_or_used": new_or_used,
            "currency_code": "NOK", "region": "europe",
        }, timeout=10)
        if r.ok:
            data = r.json().get("data")
            if data and int(data.get("total_quantity") or 0) > 0:
                return data
    except Exception:
        pass
    try:
        r = requests.get(url, auth=BL_AUTH, params={
            "guide_type": guide_type, "new_or_used": new_or_used,
            "currency_code": "USD",
        }, timeout=10)
        if not r.ok:
            return None
        data = r.json().get("data")
        if not data or int(data.get("total_quantity") or 0) == 0:
            return None
        for key in ("min_price", "max_price", "avg_price", "qty_avg_price"):
            if data.get(key):
                data[key] = str(round(float(data[key]) * _USD_TO_NOK, 2))
        data["currency_code"] = "NOK"
        return data
    except Exception:
        return None

def _fetch_raw(item_type: str, item_id: str, condition: str, guide_type: str) -> dict | None:
    """Try matching condition first, fall back to opposite."""
    new_or_used = "N" if condition == "SEALED" else "U"
    data = _fetch_one(item_type, item_id, new_or_used, guide_type)
    if data:
        return data
    opposite = "U" if new_or_used == "N" else "N"
    return _fetch_one(item_type, item_id, opposite, guide_type)


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


def _bl_name_matches(col_id: str, expected_name: str) -> bool:
    """Validate a derived BL col* ID by comparing item name with expected name."""
    if not expected_name:
        return True
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{col_id}",
            auth=BL_AUTH, timeout=8,
        )
        if not r.ok:
            return True
        bl_name = r.json().get("data", {}).get("name", "")
        if not bl_name:
            return True
        def _norm(s):
            return s.lower().replace("(cmf)", "").replace("(classic)", "").strip()
        return _norm(expected_name) in _norm(bl_name) or _norm(bl_name) in _norm(expected_name)
    except Exception:
        return True


def _rb_bl_minifig_id(set_number: str, expected_name: str = "") -> str | None:
    """
    Find the BrickLink MINIFIG id (col*) for a CMF individual figure.

    Strategy:
      1. Check set's own external_ids.BrickLink (works for older CMF series)
      2. Follow set → minifig → minifig external_ids (common for series 71011+)
      3. Derive from series mapping, then validate name via BrickLink catalog
    """
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

        derived = _cmf_derived_bl_id(set_number)
        if derived and _bl_name_matches(derived, expected_name):
            return derived
        return None
    except Exception:
        return None


def bl_get_price(set_number: str, condition: str, object_type: str = "SET",
                 name: str = "", bl_item_no: str = "") -> tuple[float | None, str | None]:
    """
    Fetch BrickLink price (NOK) and official BL name.

    Returns (price, bl_name) tuple. Either or both may be None.

    Lookup strategy:
      0. If bl_item_no is a col* ID → direct MINIFIG lookup (fastest, most reliable)
      1. MINIFIG object_type or CMF variant (suffix > 1): look up as MINIFIG via Rebrickable
      2. SET: try BrickLink SET type with base number
      3. GEAR fallback: keychains, accessories and GWP items BL lists as GEAR not SET
    """
    # ── Direct bl_item_no lookup ─────────────────────────────────────────────
    # bl_item_no is the authoritative BrickLink ID. Auto-detect item type.
    if bl_item_no:
        _bl_id = bl_item_no.lower()
        if _bl_id.startswith("col"):
            type_order = ["SET"]
        elif any(_bl_id.startswith(p) for p in (
            "cty", "sw", "hp", "sh", "njo", "tlm", "twn", "gen", "fig",
            "idea", "pi", "cas", "adv", "alp", "pha", "poc", "lor",
        )):
            type_order = ["MINIFIG", "SET", "GEAR"]
        elif any(c.isalpha() for c in _bl_id.split("-")[0][-3:]):
            type_order = ["PART", "GEAR", "MINIFIG", "SET"]
        else:
            type_order = ["SET", "GEAR", "MINIFIG", "PART"]

        for item_type in type_order:
            bl_name = _fetch_bl_name(item_type, bl_item_no)
            if bl_name is not None:
                price = _weighted_price(
                    _fetch_raw(item_type, bl_item_no, condition, "sold"),
                    _fetch_raw(item_type, bl_item_no, condition, "stock"),
                )
                return price, bl_name
        return None, None

    base, _, suffix = set_number.partition("-")
    is_cmf = suffix.isdigit() and int(suffix) > 1

    # ── MINIFIG / CMF path ────────────────────────────────────────────────────
    if object_type == "MINIFIG" or is_cmf:
        fig_id = _rb_bl_minifig_id(set_number, name)
        if fig_id:
            price = _weighted_price(
                _fetch_raw("MINIFIG", fig_id, condition, "sold"),
                _fetch_raw("MINIFIG", fig_id, condition, "stock"),
            )
            bl_name = _fetch_bl_name("MINIFIG", fig_id)
            if price:
                return price, bl_name
        return None, None

    # ── SET path ──────────────────────────────────────────────────────────────
    for item_id in (base, f"{base}-1"):
        price = _weighted_price(
            _fetch_raw("SET", item_id, condition, "sold"),
            _fetch_raw("SET", item_id, condition, "stock"),
        )
        if price:
            bl_name = _fetch_bl_name("SET", item_id)
            return price, bl_name

    # ── GEAR fallback ────────────────────────────────────────────────────────
    price = _weighted_price(
        _fetch_raw("GEAR", base, condition, "sold"),
        _fetch_raw("GEAR", base, condition, "stock"),
    )
    if price:
        bl_name = _fetch_bl_name("GEAR", base)
        return price, bl_name
    return None, None


def update_object(ownership_id: str, updates: dict):
    """Patch one or more fields on an object by ownership_id."""
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/objects",
        headers={**SB_HEADERS, "Prefer": "return=minimal"},
        params={"ownership_id": f"eq.{ownership_id}"},
        data=json.dumps(updates),
    )


if __name__ == "__main__":
    sets = fetch_all_sets()
    print(f"Synkroniserer priser for {len(sets)} sett ...")

    updated = skipped = names_updated = 0
    for i, obj in enumerate(sets):
        price, bl_name = bl_get_price(
            obj["set_number"], obj.get("condition", "USED"),
            obj.get("object_type", "SET"), obj.get("name", ""),
            obj.get("bl_item_no", ""),
        )
        patch = {}
        if price:
            patch["estimated_value_bl"] = price
        if bl_name:
            patch["name_bl"] = bl_name
            names_updated += 1
        if patch:
            update_object(obj["ownership_id"], patch)
            updated += 1
            name_info = f" | BL: {bl_name}" if bl_name else ""
            price_info = f"{price:.0f} kr" if price else "—"
            print(f"  [{i+1}/{len(sets)}] {obj['ownership_id']} {obj['set_number']}: {price_info}{name_info}")
        else:
            skipped += 1
            print(f"  [{i+1}/{len(sets)}] {obj['ownership_id']} {obj['set_number']}: ikke funnet")

        # BrickLink rate limit: stay well under 5000/dag
        time.sleep(0.5)

    print(f"\nFerdig. Oppdatert: {updated}, ikke funnet: {skipped}, navn hentet: {names_updated}")
