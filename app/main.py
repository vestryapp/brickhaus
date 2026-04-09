"""
BrickHaus – Streamlit app
Phase 1: View, search and register Lego objects.
Mobile-first registration with Rebrickable auto-fill.

Run:  streamlit run app/main.py
"""

import os
import io
import json
import html
import base64
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests_oauthlib import OAuth1
import streamlit as st
from PIL import Image, ImageDraw
import anthropic

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL    = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
REBRICKABLE_KEY     = os.environ.get("REBRICKABLE_API_KEY", "")
BL_CONSUMER_KEY     = os.environ.get("BRICKLINK_CONSUMER_KEY", "")
BL_CONSUMER_SECRET  = os.environ.get("BRICKLINK_CONSUMER_SECRET", "")
BL_TOKEN            = os.environ.get("BRICKLINK_TOKEN", "")
BL_TOKEN_SECRET     = os.environ.get("BRICKLINK_TOKEN_SECRET", "")
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

def _bl_auth():
    return OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)


_MAX_IMG_PX = 1024  # resize long edge to this before sending — reduces image tokens ~4–16×

def _resize_image(image_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    """Resize image so its longest side is at most _MAX_IMG_PX. Returns (bytes, media_type)."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img.thumbnail((_MAX_IMG_PX, _MAX_IMG_PX), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = "JPEG" if "jpeg" in content_type or "jpg" in content_type else "PNG"
        img.convert("RGB").save(buf, format=fmt, quality=85)
        return buf.getvalue(), f"image/{fmt.lower()}"
    except Exception:
        return image_bytes, content_type


def identify_lego_from_image(image_bytes: bytes, content_type: str) -> dict:
    """
    Use Claude Vision (Haiku) to identify a Lego set from an image (box or built set).
    Image is resized before sending to minimise token cost.
    Returns: {"set_number": str|None, "name": str|None, "confidence": "high"|"medium"|"low", "note": str}
    """
    if not ANTHROPIC_API_KEY:
        return {"set_number": None, "name": None, "confidence": "low",
                "note": "ANTHROPIC_API_KEY ikke satt."}
    try:
        small_bytes, media_type = _resize_image(image_bytes, content_type)
        img_b64 = base64.standard_b64encode(small_bytes).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",  # Haiku: ~12× billigere enn Sonnet, tilstrekkelig for settgjenkjenning
            max_tokens=128,                      # Vi trenger bare et lite JSON-svar
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": img_b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Du er ekspert på Lego-sett. Se på dette bildet – det kan vise en Lego-eske "
                            "eller et ferdig bygget Lego-sett.\n\n"
                            "Identifiser settnummeret hvis mulig. Svar KUN med et JSON-objekt, ingen annen tekst:\n"
                            '{"set_number": "75192", "name": "Millennium Falcon", "confidence": "high"}\n\n'
                            "Confidence-verdier: \"high\" (sikker), \"medium\" (trolig riktig), \"low\" (usikker).\n"
                            "Hvis du ikke kan identifisere settet, bruk null for set_number og name, og \"low\" for confidence.\n"
                            "Sett-nummeret skal kun inneholde tall og bindestrek, f.eks. \"75192\" eller \"71011-8\"."
                        ),
                    },
                ],
            }],
        )
        raw = msg.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        return {
            "set_number":  result.get("set_number"),
            "name":        result.get("name"),
            "confidence":  result.get("confidence", "low"),
            "note":        "",
        }
    except json.JSONDecodeError:
        return {"set_number": None, "name": None, "confidence": "low",
                "note": "Kunne ikke tolke svar fra AI."}
    except Exception as e:
        return {"set_number": None, "name": None, "confidence": "low",
                "note": f"Feil: {e}"}

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}
RB_HEADERS = {"Authorization": f"key {REBRICKABLE_KEY}"}


def _make_lego_icon(size: int = 64) -> Image.Image:
    """Generate a simple Lego brick icon (red brick, two yellow studs)."""
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    RED  = (218, 41, 28)
    DARK = (170, 25, 15)
    m    = size // 8          # margin
    top  = size // 3          # brick top edge

    # Brick body
    draw.rounded_rectangle([m, top, size - m, size - m],
                            radius=size // 10, fill=RED)
    # Studs (two circles sitting on top of the brick)
    sr = size // 9            # stud radius
    sy = top - sr + 2
    for cx in [size // 3, 2 * size // 3]:
        draw.ellipse([cx - sr, sy - sr, cx + sr, sy + sr], fill=DARK)
        draw.ellipse([cx - sr + 2, sy - sr + 2, cx + sr - 2, sy + sr - 2], fill=RED)

    return img


@st.cache_data
def _lego_icon_b64(size: int = 40) -> str:
    buf = io.BytesIO()
    _make_lego_icon(size).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


st.set_page_config(page_title="BrickHaus", page_icon=_make_lego_icon(64), layout="wide")


# ── API helpers ───────────────────────────────────────────────────────────────

def sb_get(table, params):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=SB_HEADERS, params=params)
    r.raise_for_status()
    return r.json()

def sb_post(table, data):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**SB_HEADERS, "Prefer": "return=representation"},
        data=json.dumps(data, default=str),
    )
    r.raise_for_status()
    return r.json()

def sb_patch(table, filters: dict, data: dict):
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**SB_HEADERS, "Prefer": "return=minimal"},
        params=filters,
        data=json.dumps(data, default=str),
    )
    r.raise_for_status()

def _fetch_bl_name(item_type: str, item_id: str) -> str | None:
    """Fetch the official BrickLink catalog name for an item."""
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}",
            auth=_bl_auth(), timeout=8,
        )
        if not r.ok:
            return None
        raw = r.json().get("data", {}).get("name") or None
        return html.unescape(raw) if raw else None
    except Exception:
        return None


_USD_TO_NOK = 10.5  # approximate fallback rate, updated periodically

def _bl_fetch_raw(item_type: str, item_id: str,
                  condition: str, guide_type: str) -> dict | None:
    """Single BrickLink price API call. Tries NOK/Europe first, falls back to USD global."""
    new_or_used = "N" if condition == "SEALED" else "U"
    url = f"https://api.bricklink.com/api/store/v1/items/{item_type}/{item_id}/price"
    auth = _bl_auth()

    # Try NOK / Europe first
    try:
        r = requests.get(url, auth=auth, params={
            "guide_type": guide_type, "new_or_used": new_or_used,
            "currency_code": "NOK", "region": "europe",
        }, timeout=10)
        if r.ok:
            data = r.json().get("data")
            if data and int(data.get("total_quantity") or 0) > 0:
                return data
    except Exception:
        pass

    # Fallback: USD global (no region filter) → convert to NOK
    try:
        r = requests.get(url, auth=auth, params={
            "guide_type": guide_type, "new_or_used": new_or_used,
            "currency_code": "USD",
        }, timeout=10)
        if not r.ok:
            return None
        data = r.json().get("data")
        if not data or int(data.get("total_quantity") or 0) == 0:
            return None
        # Convert USD prices to NOK
        for key in ("min_price", "max_price", "avg_price", "qty_avg_price"):
            if data.get(key):
                data[key] = str(round(float(data[key]) * _USD_TO_NOK, 2))
        data["currency_code"] = "NOK"
        return data
    except Exception:
        return None

def _weighted_price(sold: dict | None, stock: dict | None) -> float | None:
    """
    Quantity-weighted average across sold (last 6 months) and stock (current listings).
    Prevents single high-priced outlier sales from dominating when few transactions exist.
    Example: 2 sold @ 4000 + 20 in stock @ 200 → (8000+4000)/22 = 545 NOK (not 1918).

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
        # Weighted by quantity – guards against outlier sales
        weighted = (sold_avg * sold_qty + stock_avg * stock_qty) / total
        return round(weighted, 2) if weighted > 0 else None

    # total_quantity=0 but price data exists (older/rare items) – use avg directly
    if sold_avg > 0:
        return round(sold_avg, 2)
    if stock_avg > 0:
        return round(stock_avg, 2)
    return None

# Mapping from LEGO CMF base set number → BrickLink series number.
# Used to derive col* IDs when Rebrickable has no BL external_ids.
# Series 1-8: BL uses sequential col001-col128 (16 figures each).
# Series 9+:  BL uses col{series}-{variant}.
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
    """
    Derive a BrickLink col* ID from a CMF set number using the series mapping.
    Example: 71011-16 → series 15, variant 16 → 'col15-16'
             8683-3   → series 1,  variant 3  → 'col003'
    Returns None if set_number is not a known CMF series.
    """
    base, _, variant = set_number.partition("-")
    if not variant.isdigit():
        return None
    series = _CMF_SERIES_BY_BASE.get(base)
    if not series:
        return None
    var_num = int(variant)
    if series <= 8:
        # Sequential IDs: series 1 starts at col001, each series has 16 figures
        return f"col{(series - 1) * 16 + var_num:03d}"
    return f"col{series}-{variant}"


def _bl_name_matches(col_id: str, expected_name: str) -> bool:
    """
    Fetch item name from BrickLink catalog and check it matches expected_name.
    Used to validate derived col* IDs before trusting their price.
    Returns True if names match, False if they don't, True if BL name unavailable
    (fail-open: if we can't verify, we accept rather than silently drop the price).
    """
    if not expected_name:
        return True
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{col_id}",
            auth=_bl_auth(), timeout=8,
        )
        if not r.ok:
            return True  # can't verify — accept
        bl_name = r.json().get("data", {}).get("name", "")
        if not bl_name:
            return True  # no name returned — accept

        def _norm(s: str) -> str:
            return s.lower().replace("(cmf)", "").replace("(classic)", "").strip()

        n_exp = _norm(expected_name)
        n_bl  = _norm(bl_name)
        match = n_exp in n_bl or n_bl in n_exp
        return match
    except Exception:
        return True  # network error — accept rather than silently drop


def _rb_get_bl_minifig_id(set_number: str, expected_name: str = "") -> str | None:
    """
    Find the BrickLink MINIFIG id (col*) for a CMF individual figure.

    Strategy:
      1. Check set's own external_ids.BrickLink (works for older CMF series)
      2. Follow set → minifig → minifig external_ids (common for series 71011+)
      3. Derive col* ID from CMF series mapping, then validate name against BrickLink
    """
    try:
        # Step 1: try set-level external IDs (works for 8683-era series)
        r = requests.get(
            f"https://rebrickable.com/api/v3/lego/sets/{set_number}/",
            headers=RB_HEADERS, timeout=8,
        )
        if not r.ok:
            return None
        for ext in r.json().get("external_ids", {}).get("BrickLink", []):
            if str(ext).startswith("col"):
                return str(ext)

        # Step 2: follow set → minifig → minifig external IDs
        r2 = requests.get(
            f"https://rebrickable.com/api/v3/lego/sets/{set_number}/minifigs/",
            headers=RB_HEADERS, timeout=8,
        )
        if not r2.ok:
            return None
        figs = r2.json().get("results", [])
        if len(figs) != 1:
            return None  # ambiguous — not a single-figure CMF set
        fig_num = figs[0].get("set_num")  # field is "set_num" in this endpoint
        if not fig_num:
            return None
        r3 = requests.get(
            f"https://rebrickable.com/api/v3/lego/minifigs/{fig_num}/",
            headers=RB_HEADERS, timeout=8,
        )
        if not r3.ok:
            return None
        for ext in r3.json().get("external_ids", {}).get("BrickLink", []):
            if str(ext).startswith("col"):
                return str(ext)

        # Step 3: derive from series mapping and validate name via BrickLink catalog
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
      3. GEAR fallback: keychains, accessories and GWP items that BL lists as GEAR not SET
    """
    if not BL_CONSUMER_KEY:
        return None, None

    # ── Direct bl_item_no lookup (from Excel import or manual entry) ─────────
    # bl_item_no is the authoritative BrickLink ID. Determine the BL item type
    # from the ID prefix, or try all types as fallback.
    if bl_item_no:
        # Determine likely BL types from ID prefix
        _bl_id = bl_item_no.lower()
        if _bl_id.startswith("col"):
            type_order = ["SET"]                          # col* = CMF sets
        elif any(_bl_id.startswith(p) for p in (
            "cty", "sw", "hp", "sh", "njo", "tlm", "twn", "gen", "fig",
            "idea", "pi", "cas", "adv", "alp", "pha", "poc", "lor",
        )):
            type_order = ["MINIFIG", "SET", "GEAR"]       # minifig prefixes
        elif any(c.isalpha() for c in _bl_id.split("-")[0][-3:]):
            type_order = ["PART", "GEAR", "MINIFIG", "SET"]  # likely a part ID
        else:
            type_order = ["SET", "GEAR", "MINIFIG", "PART"]  # numeric = probably set

        for item_type in type_order:
            bl_name = _fetch_bl_name(item_type, bl_item_no)
            if bl_name is not None:
                # Found the right type — now get price
                price = _weighted_price(
                    _bl_fetch_raw(item_type, bl_item_no, condition, "sold"),
                    _bl_fetch_raw(item_type, bl_item_no, condition, "stock"),
                )
                return price, bl_name
        return None, None  # bl_item_no given but not found on BrickLink

    base, _, suffix = set_number.partition("-")
    is_cmf_variant = suffix.isdigit() and int(suffix) > 1

    # ── MINIFIG / CMF path ────────────────────────────────────────────────────
    # CMF variants (suffix > 1) must be priced as MINIFIG via Rebrickable BL ID.
    # Do NOT fall through to SET price — that would return the full series price.
    if object_type == "MINIFIG" or is_cmf_variant:
        bl_fig_id = _rb_get_bl_minifig_id(set_number, name)
        if bl_fig_id:
            price = _weighted_price(
                _bl_fetch_raw("MINIFIG", bl_fig_id, condition, "sold"),
                _bl_fetch_raw("MINIFIG", bl_fig_id, condition, "stock"),
            )
            bl_name = _fetch_bl_name("MINIFIG", bl_fig_id)
            if price:
                return price, bl_name
        return None, None  # No BL MINIFIG ID found — better no price than wrong price

    # ── SET path ──────────────────────────────────────────────────────────────
    # Try base number first (BrickLink standard), then with "-1" suffix.
    for item_id in (base, f"{base}-1"):
        price = _weighted_price(
            _bl_fetch_raw("SET", item_id, condition, "sold"),
            _bl_fetch_raw("SET", item_id, condition, "stock"),
        )
        if price:
            bl_name = _fetch_bl_name("SET", item_id)
            return price, bl_name

    # ── GEAR fallback (keychains, accessories, GWP items) ────────────────────
    price = _weighted_price(
        _bl_fetch_raw("GEAR", base, condition, "sold"),
        _bl_fetch_raw("GEAR", base, condition, "stock"),
    )
    if price:
        bl_name = _fetch_bl_name("GEAR", base)
        return price, bl_name
    return None, None

def rb_resolve_themes(data: dict) -> dict:
    """Resolve theme/subtheme hierarchy and add _theme_name/_subtheme_name."""
    theme_name, subtheme_name = "", ""
    theme_id = data.get("theme_id")
    if theme_id:
        tr = requests.get(
            f"https://rebrickable.com/api/v3/lego/themes/{theme_id}/",
            headers=RB_HEADERS, timeout=5,
        )
        if tr.ok:
            theme_data = tr.json()
            parent_id = theme_data.get("parent_id")
            if parent_id:
                subtheme_name = theme_data.get("name", "")
                pr = requests.get(
                    f"https://rebrickable.com/api/v3/lego/themes/{parent_id}/",
                    headers=RB_HEADERS, timeout=5,
                )
                if pr.ok:
                    theme_name = pr.json().get("name", "")
            else:
                theme_name = theme_data.get("name", "")
    data["_theme_name"]    = theme_name
    data["_subtheme_name"] = subtheme_name
    return data

def rb_search_variants(base_num: str) -> list:
    """Return all Rebrickable sets sharing the same base number (e.g. 71011)."""
    try:
        r = requests.get(
            "https://rebrickable.com/api/v3/lego/sets/",
            headers=RB_HEADERS,
            params={"search": base_num, "page_size": 50},
            timeout=10,
        )
        if not r.ok:
            return []
        results = r.json().get("results", [])
        base = base_num.split("-")[0]
        variants = [s for s in results
                    if s.get("set_num", "").split("-")[0] == base]
        def _variant_sort_key(s):
            parts = s.get("set_num", "").split("-")
            return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return sorted(variants, key=_variant_sort_key)
    except Exception:
        return []

def rb_lookup(set_number: str) -> dict | None:
    """Fetch a single known set variant (set_number must include '-X' suffix)."""
    try:
        r = requests.get(
            f"https://rebrickable.com/api/v3/lego/sets/{set_number}/",
            headers=RB_HEADERS, timeout=8,
        )
        if not r.ok:
            return None
        return rb_resolve_themes(r.json())
    except Exception:
        return None

def rb_search_mocs(query: str) -> list:
    """Search Rebrickable community MOCs by name or MOC ID."""
    try:
        r = requests.get(
            "https://rebrickable.com/api/v3/lego/mocs/",
            headers=RB_HEADERS,
            params={"search": query, "page_size": 12},
            timeout=10,
        )
        if not r.ok:
            return []
        return r.json().get("results", [])
    except Exception:
        return []


# ── Supabase helpers ──────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def fetch_objects():
    rows, offset, limit = [], 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/objects",
            headers={**SB_HEADERS, "Range-Unit": "items", "Range": f"{offset}-{offset+limit-1}"},
            params={"select": "id,ownership_id,object_type,set_number,bl_item_no,name,name_bl,theme,subtheme,year,condition,status,location_id,sub_location,estimated_value_bl,total_cost_nok,quality_level,notes,insured,purchase_price,purchase_currency,purchase_date,purchase_source,registered_at,num_parts,moc_base_set,instructions_url,instructions_storage_path,rebrickable_moc_id"},
        )
        chunk = r.json()
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
    return rows

@st.cache_data(ttl=300)
def fetch_locations():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/locations", headers=SB_HEADERS,
                     params={"select": "id,name", "order": "name.asc"})
    return r.json()

def next_ownership_id():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/objects",
        headers=SB_HEADERS,
        params={"select": "ownership_id", "order": "ownership_id.desc", "limit": 1},
    )
    rows = r.json()
    if not rows:
        return "LG-000001"
    num = int(rows[0]["ownership_id"].split("-")[1]) + 1
    return f"LG-{num:06d}"

def get_or_create_location(name: str) -> str:
    existing = sb_get("locations", {"name": f"eq.{name}", "select": "id"})
    if existing:
        return existing[0]["id"]
    created = sb_post("locations", [{"name": name}])
    return created[0]["id"]

def save_object(record: dict):
    sb_post("objects", [record])
    st.cache_data.clear()


# ── Supabase Storage helpers ──────────────────────────────────────────────────

STORAGE_BUCKET = "object-images"

def image_public_url(storage_path: str) -> str:
    return f"{SUPABASE_URL}/storage/v1/object/public/{STORAGE_BUCKET}/{storage_path}"

def fetch_object_image(object_uuid: str) -> dict | None:
    """Return the latest DOCUMENTATION image record for this object, or None."""
    try:
        rows = sb_get("images", {
            "object_id": f"eq.{object_uuid}",
            "image_type": "eq.DOCUMENTATION",
            "select":     "id,storage_path",
            "order":      "created_at.desc",
            "limit":      "1",
        })
        return rows[0] if rows else None
    except Exception:
        return None

def upload_instructions_file(ownership_id: str, file_bytes: bytes, content_type: str) -> str | None:
    """Upload a MOC instructions file to Storage. Returns storage_path or None on failure."""
    ext = content_type.split("/")[-1].replace("jpeg", "jpg")
    storage_path = f"{ownership_id}/instructions.{ext}"
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{storage_path}",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  content_type,
            "x-upsert":      "true",
        },
        data=file_bytes,
        timeout=30,
    )
    return storage_path if r.ok else None

def instructions_public_url(storage_path: str) -> str:
    return image_public_url(storage_path)  # same bucket


def save_documentation_image(object_uuid: str, ownership_id: str,
                              file_bytes: bytes, content_type: str) -> bool:
    """Upload a documentation image and save the record. Returns True on success."""
    ext = "jpg" if "jpeg" in content_type else content_type.split("/")[-1]
    storage_path = f"{ownership_id}/doc.{ext}"

    # Remove existing image (storage + db record)
    existing = fetch_object_image(object_uuid)
    if existing:
        requests.delete(
            f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{existing['storage_path']}",
            headers={"Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
        requests.delete(
            f"{SUPABASE_URL}/rest/v1/images",
            headers=SB_HEADERS,
            params={"id": f"eq.{existing['id']}"},
        )

    # Upload to Supabase Storage
    r = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{STORAGE_BUCKET}/{storage_path}",
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type":  content_type,
            "x-upsert":      "true",
        },
        data=file_bytes,
        timeout=30,
    )
    if not r.ok:
        return False

    # Save record in images table
    sb_post("images", [{
        "object_id":   object_uuid,
        "image_type":  "DOCUMENTATION",
        "storage_path": storage_path,
    }])

    # Promote quality_level to DOCUMENTED
    sb_patch("objects", {"id": f"eq.{object_uuid}"}, {"quality_level": "DOCUMENTED"})
    st.cache_data.clear()
    return True


# ── Display helpers ───────────────────────────────────────────────────────────

CONDITION_LABEL = {
    "SEALED":     "🔒 Forseglet",
    "OPENED":     "📦 Ubygget (åpnet)",
    "BUILT":      "🧱 Bygget",
    "USED":       "🔧 Brukt",
    "INCOMPLETE": "⚠️ Ufullstendig",
}
QUALITY_LABEL = {
    "BASIC":      "⚪ Basic",
    "DOCUMENTED": "🔵 Documented",
    "VERIFIED":   "🟢 Verified",
}
TYPE_LABEL = {
    "SET":            "Sett",
    "MINIFIG":        "Minifig",
    "PART":           "Del",
    "BULK_CONTAINER": "Bulk",
    "MOC":            "MOC",
    "MOD":            "Mod",
}

def fmt_nok(val):
    if val is None:
        return "–"
    return f"{int(val):,} kr".replace(",", "\u00a0")


_CMF_BASES = {"8683","8684","8803","8804","8805","8827","8831","8833",
              "71000","71001","71002","71004","71007","71008","71011","71013",
              "71018","71021","71025","71027","71029","71032","71034","71037","71038"}

def display_name(obj: dict) -> str:
    """Return trimmed BL name if available, otherwise Rebrickable name.
    BL names often have parenthetical suffixes like '(Complete Set with Stand and Accessories)'
    which we strip for display. The full name is stored in name_bl for reference."""
    bl = obj.get("name_bl")
    if bl:
        bl = html.unescape(bl)  # clean up legacy HTML entities from BL API
        # Trim at first '(' — e.g. "Queen, Series 15 (Complete Set...)" → "Queen, Series 15"
        idx = bl.find("(")
        name = bl[:idx].rstrip(", ") if idx > 0 else bl
    else:
        name = obj.get("name") or "–"
    # Sealed CMF random bag = base number or base-0
    sn = obj.get("set_number") or ""
    sn_base = sn.rsplit("-", 1)[0] if "-" in sn else sn
    if sn_base in _CMF_BASES and (sn == sn_base or sn.endswith("-0")):
        name += " (random bag)"
    return name


# ── Session state ─────────────────────────────────────────────────────────────

def init_state():
    defaults = {
        "sort_by":          "ID",
        "sort_asc":         True,
        "rb_name":          "",
        "rb_theme":         "",
        "rb_subtheme":      "",
        "rb_year":          date.today().year,
        "rb_img":           None,
        "rb_parts":         None,
        "rb_status":        None,
        "reg_step":         1,
        "reg_set_number":   "",
        "pending_record":   None,
        "confirm_no_loc":   False,
        "rb_fetch_trigger":  False,
        "rb_variants":       None,
        "reg_saved":         False,
        "reg_ownership_id":  None,
        "reg_obj_uuid":      None,
        "reg_input_mode":    None,
        "reg_ai_result":     None,
        "moc_prefill":       None,
        "moc_rb_results":    None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

def reset_registration():
    keys = ["rb_name","rb_theme","rb_subtheme","rb_img","rb_parts","rb_status",
            "reg_set_number","pending_record","confirm_no_loc","rb_fetch_trigger","rb_variants",
            "reg_saved","reg_ownership_id","reg_obj_uuid",
            "reg_input_mode","reg_ai_result","moc_prefill","moc_rb_results"]
    for k in keys:
        st.session_state[k] = None
    st.session_state["rb_year"]  = date.today().year
    st.session_state["rb_status"] = None
    st.session_state["reg_step"] = 1


# ── Edit dialog ───────────────────────────────────────────────────────────────

@st.dialog("Rediger objekt", width="large")
def edit_dialog(obj: dict, loc_list: list):
    oid = obj.get("ownership_id", "")
    st.caption(f"**{oid}** · Registrert: {obj.get('registered_at', '–')}")

    # Show BrickLink official name and IDs
    bl_full = obj.get("name_bl")
    if bl_full:
        st.caption(f"🏷️ BrickLink: {html.unescape(bl_full)}")
    sn = obj.get("set_number") or "–"
    bl_no = obj.get("bl_item_no") or ""
    id_line = f"📦 Settnr: **{sn}**"
    if bl_no and bl_no != sn:
        id_line += f"  ·  BL Item: **{bl_no}**"
    st.caption(id_line)

    col1, col2 = st.columns(2)
    with col1:
        is_moc = obj.get("object_type") in ("MOC", "MOD")
        name = st.text_input("Navn *", value=obj.get("name") or "",
                             disabled=not is_moc,
                             help="Navn kan kun redigeres for MOC og Mod. BL-navn vises automatisk i samlingsoversikten." if not is_moc else None)
        set_number_edit = st.text_input("Settnummer", value=obj.get("set_number") or "",
                                        help="Ditt logiske settnummer, f.eks. 71011-16 for Queen i Series 15.")
        bl_item_no_edit = st.text_input("BrickLink Item-nr",
                                         value=obj.get("bl_item_no") or "",
                                         help="BrickLinks eget oppslags-ID for pris og navn. "
                                              "F.eks. col15-16 for CMF, eller 6385680-1 for komplett boks. "
                                              "Brukes til pris/navn-oppslag hvis ulikt settnummer.")
        object_type = st.selectbox("Type", list(TYPE_LABEL.keys()),
                                   index=list(TYPE_LABEL.keys()).index(obj.get("object_type", "SET")),
                                   format_func=lambda x: TYPE_LABEL[x])
        theme    = st.text_input("Tema",    value=obj.get("theme") or "")
        subtheme = st.text_input("Subtema", value=obj.get("subtheme") or "")
        year     = st.number_input("År", min_value=1949, max_value=2030,
                                   value=int(obj.get("year") or date.today().year), step=1)
    with col2:
        cond_keys = list(CONDITION_LABEL.keys())
        cond_idx  = cond_keys.index(obj.get("condition", "SEALED")) if obj.get("condition") in cond_keys else 0
        condition = st.selectbox("Tilstand", cond_keys,
                                 index=cond_idx,
                                 format_func=lambda x: CONDITION_LABEL[x])
        loc_names   = [l["name"] for l in loc_list]
        current_loc = obj.get("location_name") or "– Ingen –"
        loc_options = ["– Ingen –"] + loc_names
        loc_idx     = loc_options.index(current_loc) if current_loc in loc_options else 0
        location    = st.selectbox("Lokasjon", loc_options, index=loc_idx)
        new_loc     = st.text_input("Eller ny lokasjon", placeholder="f.eks. Loft")
        sub_loc     = st.text_input("Sub-lokasjon", value=obj.get("sub_location") or "")
        notes       = st.text_area("Notater", value=obj.get("notes") or "")

    # ── Documentation image ───────────────────────────────────────────────────
    st.subheader("📷 Dokumentasjonsbilde")
    obj_uuid = obj.get("id")
    if obj_uuid:
        existing_img = fetch_object_image(obj_uuid)
        if existing_img:
            st.image(image_public_url(existing_img["storage_path"]), width=280)
            st.caption(f"🔵 Documented")
        else:
            st.caption("Ingen bilde lastet opp ennå — ⚪ Basic")
        new_img = st.file_uploader(
            "Last opp bilde (erstatter eventuelt eksisterende)",
            type=["jpg", "jpeg", "png", "webp"],
            key=f"edit_img_{oid}",
        )
        if new_img:
            if st.button("⬆️ Lagre bilde", key=f"save_img_{oid}", use_container_width=True):
                with st.spinner("Laster opp ..."):
                    ok = save_documentation_image(obj_uuid, oid, new_img.read(), new_img.type)
                if ok:
                    st.success("📷 Bilde lagret — 🔵 Documented!")
                    st.rerun()
                else:
                    st.error("Opplasting feilet. Sjekk at storage-bucket er opprettet i Supabase.")

    st.subheader("Verdi")
    est_value = st.number_input(
        "Estimert verdi (NOK)",
        value=float(obj.get("estimated_value_bl") or 0),
        min_value=0.0, step=10.0,
        help="Settes automatisk fra BrickLink, men kan overstyres manuelt",
    )

    # ── MOC / MOD-spesifikke felt ─────────────────────────────────────────────
    if obj.get("object_type") in ("MOC", "MOD"):
        st.subheader("🔧 MOC / MOD")
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            num_parts = st.number_input(
                "Antall deler",
                min_value=0, step=10,
                value=int(obj.get("num_parts") or 0),
            )
            moc_base_set = st.text_input(
                "Basert på sett (kun MOD)",
                value=obj.get("moc_base_set") or "",
                placeholder="f.eks. 75192-1",
                disabled=obj.get("object_type") != "MOD",
            )
        with col_m2:
            rebrickable_moc_id = st.text_input(
                "Rebrickable MOC-ID",
                value=obj.get("rebrickable_moc_id") or "",
                placeholder="f.eks. MOC-12345",
            )
            instructions_url = st.text_input(
                "Instruksjonslenke",
                value=obj.get("instructions_url") or "",
                placeholder="https://rebrickable.com/mocs/...",
            )

        # Show existing instruction file or upload new
        existing_instr = obj.get("instructions_storage_path")
        if existing_instr:
            st.caption(f"📐 Instruksjonsfil: [{existing_instr.split('/')[-1]}]"
                       f"({instructions_public_url(existing_instr)})")
        new_instr_file = st.file_uploader(
            "Last opp instruksjonsfil (erstatter eventuelt eksisterende)",
            type=["pdf","jpg","jpeg","png"],
            key=f"edit_instr_{oid}",
        )
    else:
        num_parts = obj.get("num_parts")
        moc_base_set = obj.get("moc_base_set")
        rebrickable_moc_id = obj.get("rebrickable_moc_id")
        instructions_url = obj.get("instructions_url")
        new_instr_file = None

    st.subheader("Kjøpsinformasjon")
    col3, col4 = st.columns(2)
    with col3:
        purchase_price = st.number_input("Kjøpspris",
                                         value=float(obj.get("purchase_price") or 0),
                                         min_value=0.0, step=1.0)
        currencies = ["NOK", "USD", "EUR", "GBP", "DKK", "SEK"]
        cur = obj.get("purchase_currency") or "NOK"
        purchase_currency = st.selectbox("Valuta", currencies,
                                         index=currencies.index(cur) if cur in currencies else 0)
    with col4:
        pd_val = obj.get("purchase_date")
        purchase_date   = st.date_input("Kjøpsdato",
                                        value=date.fromisoformat(pd_val) if pd_val else None)
        purchase_source = st.text_input("Kilde / selger",
                                        value=obj.get("purchase_source") or "")

    st.divider()
    col_del, col_save = st.columns([1, 2])
    with col_save:
        if st.button("💾 Lagre endringer", type="primary", use_container_width=True):
            if not name.strip():
                st.error("Navn er påkrevd.")
                return
            loc_name_used = new_loc.strip() or (location if location != "– Ingen –" else None)
            loc_id = get_or_create_location(loc_name_used) if loc_name_used else None
            price     = float(purchase_price) if purchase_price else None
            total_nok = price if (price and purchase_currency == "NOK") else obj.get("total_cost_nok")
            new_sn = set_number_edit.strip()
            new_bl = bl_item_no_edit.strip() or None
            sn_changed = new_sn and new_sn != (obj.get("set_number") or "")
            bl_changed = new_bl != (obj.get("bl_item_no") or None)
            updates = {
                "name":              name.strip() if is_moc else obj.get("name"),
                "set_number":        new_sn or obj.get("set_number"),
                "bl_item_no":        new_bl,
                "object_type":       object_type,
                "theme":             theme.strip() or None,
                "subtheme":          subtheme.strip() or None,
                "year":              int(year),
                "condition":         condition,
                "location_id":       loc_id,
                "sub_location":      sub_loc.strip() or None,
                "notes":             notes.strip() or None,
                "purchase_price":    price,
                "purchase_currency": purchase_currency,
                "purchase_date":     str(purchase_date) if purchase_date else None,
                "purchase_source":      purchase_source.strip() or None,
                "total_cost_nok":       total_nok,
                "estimated_value_bl":   float(est_value) if est_value else obj.get("estimated_value_bl"),
                "num_parts":            int(num_parts) if num_parts else None,
                "moc_base_set":         moc_base_set.strip() if moc_base_set else None,
                "rebrickable_moc_id":   rebrickable_moc_id.strip() if rebrickable_moc_id else None,
                "instructions_url":     instructions_url.strip() if instructions_url else None,
            }
            sb_patch("objects", {"ownership_id": f"eq.{oid}"}, updates)

            # If set_number or bl_item_no changed, clear stale BL data so it gets re-fetched
            if sn_changed or bl_changed:
                sb_patch("objects", {"ownership_id": f"eq.{oid}"},
                         {"name_bl": None, "estimated_value_bl": None})

            # Upload new instruction file if provided
            if new_instr_file:
                path = upload_instructions_file(oid, new_instr_file.read(), new_instr_file.type)
                if path:
                    sb_patch("objects", {"ownership_id": f"eq.{oid}"},
                             {"instructions_storage_path": path})

            st.cache_data.clear()
            st.rerun()


# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

st.markdown(
    f'<h1 style="display:flex;align-items:center;gap:12px">'
    f'<img src="data:image/png;base64,{_lego_icon_b64(56)}" height="48">'
    f'BrickHaus</h1>',
    unsafe_allow_html=True,
)

tab_collection, tab_register = st.tabs(["📦 Samling", "➕ Registrer"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 1: SAMLING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_collection:
    with st.spinner("Laster samling ..."):
        objects  = fetch_objects()
        loc_list = fetch_locations()

    loc_by_id = {l["id"]: l["name"] for l in loc_list}
    for obj in objects:
        obj["location_name"] = loc_by_id.get(obj.get("location_id"), "–")

    with st.sidebar:
        st.header("Filter")
        search = st.text_input("🔍 Søk", "")

        all_themes = sorted({o["theme"] for o in objects if o.get("theme")})
        sel_themes = st.multiselect("Tema", all_themes)

        all_types = sorted({o["object_type"] for o in objects if o.get("object_type")})
        sel_types = st.multiselect("Type", all_types, format_func=lambda x: TYPE_LABEL.get(x, x))

        all_conds = sorted({o["condition"] for o in objects if o.get("condition")})
        sel_conds = st.multiselect("Tilstand", all_conds, format_func=lambda x: CONDITION_LABEL.get(x, x))

        all_locs = sorted({o["location_name"] for o in objects if o.get("location_name") != "–"})
        sel_locs = st.multiselect("Lokasjon", all_locs)

    filtered = objects
    if search:
        q = search.lower()
        filtered = [o for o in filtered if
            q in (o.get("name") or "").lower() or
            q in (o.get("name_bl") or "").lower() or
            q in (o.get("theme") or "").lower() or
            q in (o.get("subtheme") or "").lower() or
            q in str(o.get("set_number") or "").lower() or
            q in (o.get("ownership_id") or "").lower()]
    if sel_themes:
        filtered = [o for o in filtered if o.get("theme") in sel_themes]
    if sel_types:
        filtered = [o for o in filtered if o.get("object_type") in sel_types]
    if sel_conds:
        filtered = [o for o in filtered if o.get("condition") in sel_conds]
    if sel_locs:
        filtered = [o for o in filtered if o.get("location_name") in sel_locs]

    n_sets      = sum(1 for o in filtered if o.get("object_type") == "SET")
    total_value = sum(o.get("estimated_value_bl") or 0 for o in filtered)

    c1, c2 = st.columns(2)
    c1.metric("Antall sett", f"{n_sets}")
    c2.metric("Estimert verdi", fmt_nok(total_value) if total_value else "–")

    # BrickLink price sync — only fills in missing prices.
    # Full refresh of all existing prices runs via the monthly Railway cron job.
    if BL_CONSUMER_KEY:
        missing_price = [o for o in objects
                         if o.get("object_type") in ("SET", "MINIFIG", "PART")
                         and (o.get("set_number") or o.get("bl_item_no"))
                         and not o.get("estimated_value_bl")]
        if missing_price:
            st.caption(f"⚠️ {len(missing_price)} sett mangler pris — klikk raden for å sette manuelt, eller hent automatisk")
            if st.button("🔄 Hent manglende BrickLink-priser", type="secondary",
                         help="Henter kun priser som mangler. Eksisterende priser oppdateres automatisk av månedlig synkronisering."):
                progress = st.progress(0, text="Henter priser ...")
                updated, no_data = 0, []
                for i, obj in enumerate(missing_price):
                    price, bl_name = bl_get_price(obj["set_number"], obj.get("condition", "USED"), obj.get("object_type", "SET"), obj.get("name", ""), obj.get("bl_item_no", ""))
                    if price or bl_name:
                        patch = {"valuation_date": str(date.today())}
                        if price:
                            patch["estimated_value_bl"] = price
                        if bl_name:
                            patch["name_bl"] = bl_name
                        sb_patch("objects",
                                 {"ownership_id": f"eq.{obj['ownership_id']}"},
                                 patch)
                        if price:
                            updated += 1
                        else:
                            no_data.append(f"{obj['ownership_id']} – {obj.get('name','')}")
                    else:
                        no_data.append(f"{obj['ownership_id']} – {obj.get('name','')}")
                    progress.progress((i + 1) / len(missing_price),
                                      text=f"Henter {i+1}/{len(missing_price)} ...")
                progress.empty()
                st.cache_data.clear()
                st.success(f"✅ Hentet pris for {updated} av {len(missing_price)} sett")
                if no_data:
                    with st.expander(f"⚠️ {len(no_data)} sett fikk ikke pris — sjekk manuelt"):
                        for s in no_data:
                            st.caption(s)
                st.rerun()

    # BrickLink name backfill — fetch official BL names for objects that don't have one yet
    if BL_CONSUMER_KEY:
        missing_bl_name = [o for o in objects
                           if o.get("object_type") in ("SET", "MINIFIG", "PART")
                           and (o.get("set_number") or o.get("bl_item_no"))
                           and not o.get("name_bl")]
        if missing_bl_name:
            if st.button(f"🏷️ Hent BrickLink-navn for {len(missing_bl_name)} objekter", type="secondary",
                         help="Henter offisielle BrickLink-navn for objekter som mangler dette."):
                progress = st.progress(0, text="Henter navn ...")
                names_ok, names_fail = 0, 0
                for i, obj in enumerate(missing_bl_name):
                    _, bl_name = bl_get_price(obj["set_number"], obj.get("condition", "USED"),
                                              obj.get("object_type", "SET"), obj.get("name", ""), obj.get("bl_item_no", ""))
                    if bl_name:
                        sb_patch("objects",
                                 {"ownership_id": f"eq.{obj['ownership_id']}"},
                                 {"name_bl": bl_name})
                        names_ok += 1
                    else:
                        names_fail += 1
                    progress.progress((i + 1) / len(missing_bl_name),
                                      text=f"Henter {i+1}/{len(missing_bl_name)} ...")
                progress.empty()
                st.cache_data.clear()
                st.success(f"✅ Hentet navn for {names_ok} objekter"
                           + (f" ({names_fail} ikke funnet)" if names_fail else ""))
                st.rerun()

    # Re-fetch BL names for CMF figures that have bl_item_no but a stale/generic name
    if BL_CONSUMER_KEY:
        stale_cmf = [o for o in objects
                     if (o.get("bl_item_no") or "").startswith("col")
                     and o.get("name_bl")
                     and "Complete Random Set" in html.unescape(o.get("name_bl") or "")]
        if stale_cmf:
            if st.button(f"🔄 Oppdater BL-navn for {len(stale_cmf)} CMF-figurer (feil serie-navn)",
                         type="secondary",
                         help="Disse har generisk serie-navn i stedet for figurnavn. Klikk for å hente riktig navn via col*-ID."):
                progress = st.progress(0, text="Oppdaterer CMF-navn ...")
                ok, fail = 0, 0
                details = []
                for i, obj in enumerate(stale_cmf):
                    col_id = obj["bl_item_no"]
                    # col*-IDs are SET type on BrickLink, not MINIFIG
                    bl_name = _fetch_bl_name("SET", col_id)
                    if bl_name and "Complete Random Set" not in bl_name:
                        sb_patch("objects",
                                 {"ownership_id": f"eq.{obj['ownership_id']}"},
                                 {"name_bl": bl_name})
                        ok += 1
                        details.append(f"✅ {col_id} → {bl_name}")
                    else:
                        fail += 1
                        details.append(f"❌ {col_id} → {debug}")
                    progress.progress((i + 1) / len(stale_cmf),
                                      text=f"Oppdaterer {i+1}/{len(stale_cmf)} ...")
                progress.empty()
                if ok:
                    st.success(f"✅ Oppdatert navn for {ok} CMF-figurer")
                if fail:
                    st.warning(f"⚠️ {fail} figurer fikk ikke riktig navn")
                with st.expander("Detaljer", expanded=True):
                    for d in details:
                        st.caption(d)
                st.stop()

    # Flag CMF figures registered without variant suffix (bare base number, no -0 or -N)
    cmf_no_suffix = [o for o in objects
                     if o.get("set_number") and o["set_number"] in _CMF_BASES
                     and "-" not in o["set_number"]]
    if cmf_no_suffix:
        # Identified = has a bl_item_no OR a specific name (not generic series name)
        _generic_names = {"Complete Random Set", "Innhold ukjent", "Ukjent", "Unknown"}
        def _is_identified(o):
            if o.get("bl_item_no"):
                return True
            n = o.get("name") or ""
            return n and not any(g in n for g in _generic_names) and not n.startswith("Minifigure, Series")
        cmf_identified = [o for o in cmf_no_suffix if _is_identified(o)]
        cmf_random     = [o for o in cmf_no_suffix if not _is_identified(o)]

        if cmf_random:
            with st.expander(f"📦 {len(cmf_random)} forseglede CMF random bags uten suffiks"):
                st.caption("Disse er forseglede/ukjente CMF-er. Klikk for å sette riktig suffiks: "
                           "Settnr → «8683-0» (Brickset-konvensjon), "
                           "BL Item-nr → «8683-1» (BrickLink random bag).")
                if st.button(f"📦 Sett -0 / -1 på {len(cmf_random)} random bags",
                             type="secondary"):
                    for obj in cmf_random:
                        base = obj["set_number"]
                        sb_patch("objects",
                                 {"ownership_id": f"eq.{obj['ownership_id']}"},
                                 {"set_number": f"{base}-0",
                                  "bl_item_no": f"{base}-1",
                                  "name_bl": None,
                                  "estimated_value_bl": None})
                    st.cache_data.clear()
                    st.success(f"✅ Oppdatert {len(cmf_random)} random bags: settnr -0, BL -1")
                    st.rerun()
                for o in cmf_random:
                    st.caption(f"{o['ownership_id']} – {o.get('name', '–')} ({o['set_number']})")

        if cmf_identified:
            with st.expander(f"🔍 {len(cmf_identified)} identifiserte CMF-figurer trenger riktig suffiks"):
                st.caption("Disse har et kjent figurnavn men mangler variant-suffiks i settnummeret. "
                           "Klikk raden i tabellen for å sette riktig suffiks manuelt, "
                           "f.eks. «8803» → «8803-15» for Rapper.")
                for o in cmf_identified:
                    bl = o.get("bl_item_no") or ""
                    lbl = f"{o['ownership_id']} – {o.get('name', '–')} ({o['set_number']})"
                    if bl:
                        lbl += f" · BL: {bl}"
                    st.caption(lbl)

    st.divider()

    if not filtered:
        st.info("Ingen objekter matcher filteret.")
    else:
        # Persistent sort controls — this is the only sort mechanism (dataframe column
        # sorting resets on every rerun, so we disable it by not relying on it).
        sort_cols = {
            "Status": "_status", "ID": "ownership_id", "Settnr.": "set_number",
            "Navn": "_display_name", "Tema": "theme", "År": "year",
            "Verdi": "estimated_value_bl", "Tilstand": "condition",
            "Lokasjon": "location_name", "Type": "object_type",
        }
        scol1, scol2, scol3 = st.columns([3, 1, 1])
        with scol1:
            sort_by = st.selectbox("Sorter etter", list(sort_cols.keys()),
                                   index=list(sort_cols.keys()).index(
                                       st.session_state.get("sort_by", "ID")),
                                   key="sort_by_select", label_visibility="collapsed")
        with scol2:
            sort_dir = st.selectbox("Retning", ["↑ Stigende", "↓ Synkende"],
                                    index=0 if st.session_state.get("sort_asc", True) else 1,
                                    key="sort_dir_select", label_visibility="collapsed")
        sort_asc = sort_dir.startswith("↑")
        st.session_state["sort_by"] = sort_by
        st.session_state["sort_asc"] = sort_asc

        # Pre-compute status for sorting
        def _row_status(o):
            issues = []
            if not o.get("estimated_value_bl"):
                issues.append("pris")
            if not o.get("name_bl"):
                issues.append("BL-navn")
            sn = o.get("set_number") or ""
            if sn in _CMF_BASES and "-" not in sn:
                issues.append("variant-suffiks")
            if not issues:
                return "✅"
            return "⚠️ " + ", ".join(issues)

        # Sort filtered list
        _sk = sort_cols[sort_by]
        def _sort_key(o):
            if _sk == "_display_name":
                return display_name(o).lower()
            if _sk == "_status":
                return _row_status(o)
            v = o.get(_sk)
            if v is None:
                return "" if _sk not in ("year", "estimated_value_bl") else 0
            return v if not isinstance(v, str) else v.lower()
        filtered.sort(key=_sort_key, reverse=not sort_asc)

        st.caption("Klikk en rad for å se detaljer og redigere.")

        rows = [{
            "Status":   _row_status(o),
            "ID":       o.get("ownership_id", ""),
            "Type":     TYPE_LABEL.get(o.get("object_type", ""), ""),
            "Settnr.":  o.get("set_number") or "–",
            "Navn":     display_name(o),
            "Tema":     o.get("theme") or "–",
            "År":       o.get("year") or "–",
            "Tilstand": CONDITION_LABEL.get(o.get("condition", ""), "–"),
            "Kvalitet": QUALITY_LABEL.get(o.get("quality_level", ""), "–"),
            "Lokasjon": o.get("location_name", "–"),
            "Verdi":    fmt_nok(o.get("estimated_value_bl")),
            "Kostpris": fmt_nok(o.get("total_cost_nok")),
            "Notater":  o.get("notes") or "",
        } for o in filtered]
        event = st.dataframe(
            rows,
            use_container_width=True,
            height=600,
            on_select="rerun",
            selection_mode="single-row",
            column_config={
                "Status": st.column_config.TextColumn(width="small"),
                "Notater": st.column_config.TextColumn(width="medium"),
            },
        )
        selected = event.selection.rows
        if selected:
            obj = filtered[selected[0]]
            obj["location_name"] = loc_by_id.get(obj.get("location_id"), "– Ingen –")
            edit_dialog(obj, loc_list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2: REGISTRER — mobile-first, step-by-step
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_register:

    step = st.session_state["reg_step"]

    def _progress_indicator(step):
        steps = ["Settnummer", "Detaljer", "Plassering", "Kjøp", "Lagre"]
        cols  = st.columns(len(steps))
        for i, (col, label) in enumerate(zip(cols, steps), start=1):
            if i < step:
                col.markdown(f"<div style='text-align:center;color:#2E5FA3'>✓ {label}</div>",
                             unsafe_allow_html=True)
            elif i == step:
                col.markdown(f"<div style='text-align:center;font-weight:bold'>▶ {label}</div>",
                             unsafe_allow_html=True)
            else:
                col.markdown(f"<div style='text-align:center;color:#aaa'>{label}</div>",
                             unsafe_allow_html=True)

    # ── STEP 1: Choose input mode + identify ─────────────────────────────────
    if step == 1:

        def _apply_rb_data(data: dict):
            st.session_state["rb_name"]     = data.get("name", "")
            st.session_state["rb_theme"]    = data.get("_theme_name", "")
            st.session_state["rb_subtheme"] = data.get("_subtheme_name", "")
            st.session_state["rb_year"]     = data.get("year", date.today().year)
            st.session_state["rb_img"]      = data.get("set_img_url")
            st.session_state["rb_parts"]    = data.get("num_parts")
            st.session_state["rb_status"]   = "found"
            st.session_state["rb_variants"] = None
            st.session_state["reg_step"]    = 2

        def _do_rb_fetch(raw: str):
            base = raw.strip().split("-")[0]
            variants = rb_search_variants(base)
            if not variants:
                st.session_state["rb_status"]   = "not_found"
                st.session_state["rb_variants"] = None
            elif len(variants) == 1:
                data = rb_lookup(variants[0]["set_num"])
                if data:
                    _apply_rb_data(data)
                else:
                    st.session_state["rb_status"] = "not_found"
            else:
                st.session_state["rb_status"]   = "multiple"
                st.session_state["rb_variants"] = variants

        input_mode = st.session_state.get("reg_input_mode")

        # ── Mode selector ─────────────────────────────────────────────────────
        if input_mode is None:
            st.subheader("Registrer nytt objekt")
            c1, c2, c3 = st.columns(3)
            with c1:
                if st.button("📷  Bilde", use_container_width=True,
                             type="primary", disabled=not ANTHROPIC_API_KEY):
                    st.session_state["reg_input_mode"] = "image"
                    st.rerun()
                st.caption("Gjenkjenn fra foto" if ANTHROPIC_API_KEY else "Krever ANTHROPIC_API_KEY")
            with c2:
                if st.button("🔢  Settnummer", use_container_width=True, type="primary"):
                    st.session_state["reg_input_mode"] = "number"
                    st.rerun()
                st.caption("Offisielle sett og CMF")
            with c3:
                if st.button("🔧  MOC / MOD", use_container_width=True, type="primary"):
                    st.session_state["reg_input_mode"] = "moc"
                    st.rerun()
                st.caption("Egne bygg og modifikasjoner")
            st.divider()
            _progress_indicator(step)

        # ── Image path ────────────────────────────────────────────────────────
        elif input_mode == "image":
            st.subheader("📷 Identifiser med bilde")
            st.caption("Last opp bilde av eske eller ferdig bygget sett")

            ai_result = st.session_state.get("reg_ai_result")

            if ai_result is None:
                img_file = st.file_uploader(
                    "Velg bilde",
                    type=["jpg", "jpeg", "png", "webp"],
                    key="reg_id_img",
                    label_visibility="collapsed",
                )
                if img_file:
                    if st.button("🔍 Identifiser sett", type="primary",
                                 use_container_width=True):
                        with st.spinner("Analyserer bilde ..."):
                            result = identify_lego_from_image(
                                img_file.read(), img_file.type)
                        st.session_state["reg_ai_result"] = result
                        st.rerun()
            else:
                conf = ai_result.get("confidence", "low")
                sett = ai_result.get("set_number")
                name = ai_result.get("name")

                if sett and conf in ("high", "medium"):
                    conf_label = "🟢 Høy sikkerhet" if conf == "high" else "🟡 Middels sikkerhet"
                    st.success(f"**Gjenkjent:** {name} ({sett}) — {conf_label}")
                    col_yes, col_no = st.columns(2)
                    with col_yes:
                        if st.button("✅ Stemmer — fortsett", type="primary",
                                     use_container_width=True):
                            st.session_state["reg_set_number"] = sett
                            with st.spinner("Henter fra Rebrickable ..."):
                                _do_rb_fetch(sett)
                            st.rerun()
                    with col_no:
                        if st.button("❌ Stemmer ikke", use_container_width=True):
                            st.session_state["reg_ai_result"]  = None
                            st.session_state["reg_input_mode"] = "number"
                            st.rerun()
                else:
                    st.warning("Kunne ikke identifisere settet med tilstrekkelig sikkerhet.")
                    if ai_result.get("note"):
                        st.caption(ai_result["note"])
                    elif sett:
                        st.caption(f"Beste gjett: {name} ({sett}) — for usikkert til å fortsette automatisk.")
                    if st.button("🔢 Gå til manuelt settnummer", type="primary",
                                 use_container_width=True):
                        if sett:
                            st.session_state["reg_set_number"] = sett
                        st.session_state["reg_ai_result"]  = None
                        st.session_state["reg_input_mode"] = "number"
                        st.rerun()

            if st.button("← Tilbake", use_container_width=True):
                st.session_state["reg_ai_result"]  = None
                st.session_state["reg_input_mode"] = None
                st.rerun()
            st.divider()
            _progress_indicator(step)

        # ── Number path ───────────────────────────────────────────────────────
        elif input_mode == "number":
            st.subheader("🔢 Settnummer")

            def _on_set_num_change():
                st.session_state["rb_fetch_trigger"] = True

            set_num = st.text_input(
                "Settnummer",
                value=st.session_state["reg_set_number"] or "",
                placeholder="f.eks. 75192",
                key="_set_num_input",
                on_change=_on_set_num_change,
            )

            if st.session_state.get("rb_fetch_trigger") and set_num.strip():
                st.session_state["rb_fetch_trigger"] = False
                st.session_state["reg_set_number"] = set_num.strip()
                with st.spinner("Henter ..."):
                    _do_rb_fetch(set_num.strip())
                st.rerun()

            col_a, col_b = st.columns([3, 1])
            with col_a:
                if st.button("🔍 Hent info fra Rebrickable", use_container_width=True,
                             type="primary", disabled=not set_num.strip()):
                    st.session_state["rb_fetch_trigger"] = False
                    st.session_state["reg_set_number"] = set_num.strip()
                    with st.spinner("Henter ..."):
                        _do_rb_fetch(set_num.strip())
                    st.rerun()
            with col_b:
                if st.button("← Tilbake", use_container_width=True):
                    st.session_state["reg_input_mode"] = None
                    st.rerun()

            # ── Variant gallery ───────────────────────────────────────────────
            if st.session_state["rb_status"] == "multiple":
                variants = st.session_state["rb_variants"] or []
                st.info(f"Fant {len(variants)} varianter — velg riktig:")
                cols_per_row = 4
                for row_start in range(0, len(variants), cols_per_row):
                    row_variants = variants[row_start:row_start + cols_per_row]
                    cols = st.columns(cols_per_row)
                    for col, v in zip(cols, row_variants):
                        with col:
                            with st.container(border=True):
                                if v.get("set_img_url"):
                                    st.image(v["set_img_url"], use_container_width=True)
                                st.caption(f"**{v.get('name', '')}**  \n{v.get('set_num', '')}")
                                if st.button("Velg", key=f"pick_{v['set_num']}",
                                             use_container_width=True):
                                    with st.spinner("Henter detaljer ..."):
                                        data = rb_lookup(v["set_num"])
                                    if data:
                                        _apply_rb_data(data)
                                        st.session_state["reg_set_number"] = v["set_num"]
                                    st.rerun()

            elif st.session_state["rb_status"] == "not_found":
                st.warning("Ikke funnet i Rebrickable — gå videre og fyll inn manuelt.")
                if st.button("Gå videre →", type="primary"):
                    st.session_state["reg_step"] = 2
                    st.rerun()

            st.divider()
            _progress_indicator(step)

        # ── MOC / MOD path ────────────────────────────────────────────────────
        elif input_mode == "moc":
            st.subheader("🔧 MOC / MOD")

            moc_type = st.radio("Type bygg", ["MOC – eget bygg", "MOD – modifisert sett"],
                                horizontal=True, key="moc_type_radio")
            is_mod = moc_type.startswith("MOD")

            st.divider()

            # Rebrickable MOC-søk (valgfritt)
            with st.expander("🔍 Finn i Rebrickable MOC-katalog (valgfritt)"):
                moc_query = st.text_input("Søk etter MOC-navn eller MOC-ID",
                                          placeholder="f.eks. 'Sopwith Camel' eller 'MOC-12345'",
                                          key="moc_rb_query")
                if st.button("Søk", key="moc_rb_search", disabled=not moc_query.strip()):
                    with st.spinner("Søker i Rebrickable ..."):
                        results = rb_search_mocs(moc_query.strip())
                    st.session_state["moc_rb_results"] = results
                    st.rerun()

                rb_results = st.session_state.get("moc_rb_results", [])
                if rb_results:
                    st.caption(f"Fant {len(rb_results)} treff:")
                    for m in rb_results[:6]:
                        col_img, col_info, col_btn = st.columns([1, 4, 1])
                        with col_img:
                            if m.get("moc_img_url"):
                                st.image(m["moc_img_url"], width=60)
                        with col_info:
                            st.markdown(f"**{m.get('name','')}**  \n"
                                        f"{m.get('moc_id','')} · {m.get('num_parts','')} deler")
                        with col_btn:
                            if st.button("Velg", key=f"moc_pick_{m.get('moc_id','')}",
                                         use_container_width=True):
                                st.session_state["moc_prefill"] = m
                                st.session_state["moc_rb_results"] = []
                                st.rerun()
                elif st.session_state.get("moc_rb_results") is not None and moc_query:
                    st.caption("Ingen treff — fyll inn manuelt nedenfor.")

            # Prefill from Rebrickable result
            prefill = st.session_state.get("moc_prefill") or {}

            name = st.text_input("Navn *",
                                 value=prefill.get("name", ""),
                                 placeholder="f.eks. Sopwith Camel 1:24")
            if is_mod:
                base_set = st.text_input("Basert på sett",
                                         placeholder="f.eks. 75192-1",
                                         help="Settnummeret til originalsettet som er modifisert")
            else:
                base_set = None

            col1, col2 = st.columns(2)
            with col1:
                theme   = st.text_input("Tema", value=prefill.get("theme","") or "")
                year    = st.number_input("År bygget", min_value=1949, max_value=2030,
                                          value=date.today().year, step=1)
                num_parts = st.number_input(
                    "Antall deler (estimat)",
                    min_value=0, step=10,
                    value=int(prefill.get("num_parts") or 0),
                    help="Kan oppdateres senere",
                )
            with col2:
                condition = st.selectbox("Tilstand",
                                         list(CONDITION_LABEL.keys()),
                                         index=list(CONDITION_LABEL.keys()).index("BUILT"),
                                         format_func=lambda x: CONDITION_LABEL[x])
                notes = st.text_area("Notater", placeholder="Fri tekst ...")

            # Instructions
            st.subheader("📐 Instruksjoner")
            instructions_url = st.text_input(
                "Lenke til instruksjoner",
                value=prefill.get("rebrickable_moc_url","") or "",
                placeholder="f.eks. https://rebrickable.com/mocs/MOC-12345/",
            )
            instructions_file = st.file_uploader(
                "Last opp instruksjonsfil (PDF, bilde)",
                type=["pdf","jpg","jpeg","png"],
                key="moc_instr_file",
            )

            moc_id = prefill.get("moc_id","") or ""

            col_back, col_next = st.columns(2)
            with col_back:
                if st.button("← Tilbake", use_container_width=True):
                    st.session_state["reg_input_mode"] = None
                    st.session_state["moc_prefill"]    = None
                    st.session_state["moc_rb_results"] = None
                    st.rerun()
            with col_next:
                if st.button("Neste →", use_container_width=True, type="primary"):
                    if not name.strip():
                        st.error("Navn er påkrevd.")
                    else:
                        obj_type = "MOD" if is_mod else "MOC"
                        st.session_state["pending_record"] = {
                            "object_type":       obj_type,
                            "set_number":        None,
                            "name":              name.strip(),
                            "theme":             theme.strip() or None,
                            "subtheme":          None,
                            "year":              int(year),
                            "condition":         condition,
                            "notes":             notes.strip() or None,
                            "num_parts":         int(num_parts) if num_parts else None,
                            "moc_base_set":      base_set.strip() if base_set else None,
                            "instructions_url":  instructions_url.strip() or None,
                            "rebrickable_moc_id": moc_id or None,
                            "_moc_instr_file":   instructions_file,
                        }
                        st.session_state["reg_step"] = 3
                        st.rerun()

    # ── STEP 2: Details ───────────────────────────────────────────────────────
    elif step == 2:
        _progress_indicator(step)
        st.divider()
        st.subheader("Detaljer")

        if st.session_state["rb_status"] == "found" and st.session_state["rb_img"]:
            c1, c2 = st.columns([1, 3])
            with c1:
                st.image(st.session_state["rb_img"], width=100)
            with c2:
                st.success(f"**{st.session_state['rb_name']}** · "
                           f"{st.session_state['rb_theme']} · "
                           f"{st.session_state['rb_year']}")
                st.caption("Data hentet fra [Rebrickable](https://rebrickable.com)")

        object_type = st.selectbox("Type", list(TYPE_LABEL.keys()),
                                   format_func=lambda x: TYPE_LABEL[x])
        name        = st.text_input("Navn *", value=st.session_state["rb_name"])
        theme       = st.text_input("Tema",   value=st.session_state["rb_theme"])
        subtheme    = st.text_input("Subtema", value=st.session_state["rb_subtheme"])
        year        = st.number_input("År", min_value=1949, max_value=2030,
                                      value=int(st.session_state["rb_year"]), step=1)
        condition   = st.selectbox(
            "Tilstand *",
            list(CONDITION_LABEL.keys()),
            format_func=lambda x: CONDITION_LABEL[x],
            help="'Brukt' = bygget og lekt med, synlig slitasje",
        )
        notes       = st.text_area("Notater", placeholder="Fri tekst ...")

        col_back, col_next = st.columns(2)
        with col_back:
            if st.button("← Tilbake", use_container_width=True):
                st.session_state["reg_step"] = 1
                st.rerun()
        with col_next:
            if st.button("Neste →", use_container_width=True, type="primary"):
                if not name.strip():
                    st.error("Navn er påkrevd.")
                else:
                    st.session_state["pending_record"] = {
                        "object_type": object_type,
                        "set_number":  st.session_state["reg_set_number"] or None,
                        "name":        name.strip(),
                        "theme":       theme.strip() or None,
                        "subtheme":    subtheme.strip() or None,
                        "year":        int(year),
                        "condition":   condition,
                        "notes":       notes.strip() or None,
                    }
                    st.session_state["reg_step"] = 3
                    st.rerun()

    # ── STEP 3: Location ──────────────────────────────────────────────────────
    elif step == 3:
        _progress_indicator(step)
        st.divider()
        st.subheader("Plassering")

        loc_list_reg = fetch_locations()
        loc_names    = [l["name"] for l in loc_list_reg]

        location_choice = st.selectbox("Lokasjon", ["– Ingen / vet ikke –"] + loc_names)
        new_location    = st.text_input("Eller skriv inn ny lokasjon",
                                        placeholder="f.eks. Kjeller")
        sub_location    = st.text_input("Sub-lokasjon", placeholder="f.eks. Hylle 3")

        col_back, col_next = st.columns(2)
        with col_back:
            if st.button("← Tilbake", use_container_width=True):
                st.session_state["reg_step"] = 2
                st.rerun()
        with col_next:
            if st.button("Neste →", use_container_width=True, type="primary"):
                loc_name_used = (new_location.strip() or
                                 (location_choice if location_choice != "– Ingen / vet ikke –" else None))
                st.session_state["pending_record"]["_loc_name"] = loc_name_used
                st.session_state["pending_record"]["sub_location"] = sub_location.strip() or None
                st.session_state["confirm_no_loc"] = loc_name_used is None
                st.session_state["reg_step"] = 4
                st.rerun()

        if st.session_state.get("confirm_no_loc"):
            st.warning("💡 Du er i ferd med å lagre uten å registrere plassering.")

    # ── STEP 4: Purchase ──────────────────────────────────────────────────────
    elif step == 4:
        _progress_indicator(step)
        st.divider()
        st.subheader("Kjøpsinformasjon")
        st.caption("Alle felt er valgfrie")

        if st.session_state.get("confirm_no_loc"):
            st.warning("⚠️ Vil du lagre uten å registrere plassering?")

        purchase_price    = st.number_input("Kjøpspris", min_value=0.0, step=1.0)
        purchase_currency = st.selectbox("Valuta", ["NOK", "USD", "EUR", "GBP", "DKK", "SEK"])
        purchase_date     = st.date_input("Kjøpsdato (valgfri)", value=None)
        purchase_source   = st.text_input("Kilde / selger",
                                          placeholder="f.eks. BrickLink, LEGO.com")
        st.caption(f"📅 Registreres: {date.today().strftime('%d.%m.%Y')}")

        price     = float(purchase_price) if purchase_price else None
        total_nok = price if (price and purchase_currency == "NOK") else None

        st.session_state["pending_record"].update({
            "purchase_price":    price,
            "purchase_currency": purchase_currency,
            "purchase_date":     str(purchase_date) if purchase_date else None,
            "purchase_source":   purchase_source.strip() or None,
            "total_cost_nok":    total_nok,
        })

        col_back, col_save = st.columns(2)
        with col_back:
            if st.button("← Tilbake", use_container_width=True):
                st.session_state["reg_step"] = 3
                st.rerun()
        with col_save:
            save_label = "💾 Lagre uten plassering" if st.session_state.get("confirm_no_loc") else "💾 Lagre"
            if st.button(save_label, use_container_width=True, type="primary"):
                st.session_state["reg_step"] = 5
                st.rerun()

    # ── STEP 5: Save + optional image ────────────────────────────────────────
    elif step == 5:
        # Guard: only save once even if Streamlit reruns (e.g. during image upload)
        if not st.session_state.get("reg_saved"):
            rec = st.session_state["pending_record"].copy()
            with st.spinner("Lagrer ..."):
                loc_name      = rec.pop("_loc_name", None)
                instr_file    = rec.pop("_moc_instr_file", None)
                loc_id        = get_or_create_location(loc_name) if loc_name else None

                bl_price, bl_name = None, None
                if rec.get("set_number") and rec.get("object_type") in ("SET", "MINIFIG"):
                    bl_price, bl_name = bl_get_price(rec["set_number"], rec.get("condition", "USED"), rec.get("object_type", "SET"), rec.get("name", ""))

                ownership_id = next_ownership_id()
                record = {
                    **rec,
                    "ownership_id":       ownership_id,
                    "status":             "OWNED",
                    "location_id":        loc_id,
                    "registered_at":      str(date.today()),
                    "quality_level":      "BASIC",
                    "estimated_value_bl": bl_price,
                    "name_bl":            bl_name,
                }
                save_object(record)

                # Upload instruction file for MOC/MOD if provided
                if instr_file:
                    path = upload_instructions_file(
                        ownership_id, instr_file.read(), instr_file.type)
                    if path:
                        sb_patch("objects",
                                 {"ownership_id": f"eq.{ownership_id}"},
                                 {"instructions_storage_path": path})

                # Fetch UUID for image linking
                try:
                    uuid_rows = sb_get("objects", {"ownership_id": f"eq.{ownership_id}", "select": "id"})
                    obj_uuid  = uuid_rows[0]["id"] if uuid_rows else None
                except Exception:
                    obj_uuid = None

                st.session_state["reg_saved"]       = True
                st.session_state["reg_ownership_id"] = ownership_id
                st.session_state["reg_obj_uuid"]     = obj_uuid

        ownership_id = st.session_state.get("reg_ownership_id", "")
        obj_uuid     = st.session_state.get("reg_obj_uuid")
        saved_name   = st.session_state["pending_record"].get("name", "")

        st.success(f"✅ **{saved_name}** lagret som **{ownership_id}**")

        # Optional documentation image
        st.subheader("📷 Legg til bilde (valgfri)")
        st.caption("Laster du opp et bilde, oppgraderes kvalitetsnivået til 🔵 Documented automatisk.")
        img_file = st.file_uploader(
            "Velg bilde",
            type=["jpg", "jpeg", "png", "webp"],
            key="reg_img_upload",
            label_visibility="collapsed",
        )
        if img_file and obj_uuid:
            if st.button("⬆️ Last opp bilde", use_container_width=True):
                with st.spinner("Laster opp ..."):
                    ok = save_documentation_image(obj_uuid, ownership_id,
                                                  img_file.read(), img_file.type)
                if ok:
                    st.success("📷 Bilde lagret — 🔵 Documented!")
                else:
                    st.error("Opplasting feilet. Sjekk at storage-bucket er opprettet i Supabase.")

        if st.button("➕ Registrer et til", type="primary", use_container_width=True):
            reset_registration()
            st.rerun()
