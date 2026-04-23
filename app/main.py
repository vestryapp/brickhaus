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
import hashlib as _hashlib
import base64 as _b64
import secrets as _secrets
import urllib.parse as _urlparse
import streamlit as st
from PIL import Image, ImageDraw
import anthropic
from supabase import create_client as _sb_create_client

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL      = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY      = os.environ["SUPABASE_SERVICE_KEY"]
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
APP_URL           = os.environ.get("APP_URL", "http://localhost:8501")
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
    """Resize image so its longest side is at most _MAX_IMG_PX.
    Also corrects EXIF orientation so mobile photos are right-side up.
    Returns (bytes, media_type).
    """
    try:
        from PIL import ImageOps
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)   # fix mobile rotation (EXIF tag 274)
        img.thumbnail((_MAX_IMG_PX, _MAX_IMG_PX), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = "JPEG" if "jpeg" in content_type or "jpg" in content_type else "PNG"
        img.convert("RGB").save(buf, format=fmt, quality=85)
        return buf.getvalue(), f"image/{fmt.lower()}"
    except Exception:
        return image_bytes, content_type


def identify_lego_from_image(image_bytes: bytes, content_type: str) -> dict:
    """
    Use Claude Vision (Haiku) to identify any LEGO object from an image.
    Handles sets, minifigs, parts, bulk, boxes, instructions, etc.
    Returns: {
      "type_guess": str,           # SET|MINIFIG|PART|BULK|INSTRUCTION|BOX|GEAR|MOC|CATALOG|OTHER
      "set_number": str|None,      # set/part number if identified
      "name": str|None,
      "year": int|None,
      "wear_level": str|None,
      "wear_note": str|None,
      "part_description": str|None,  # for PART: "2x4 brick, red" style description for RB search
      "part_search_query": str|None, # short search term for Rebrickable parts API
      "confidence": str,           # "high"|"medium"|"low"
      "note": str
    }
    """
    if not ANTHROPIC_API_KEY:
        return {"type_guess": "OTHER", "set_number": None, "name": None, "year": None,
                "wear_level": None, "wear_note": None, "part_description": None,
                "part_search_query": None, "confidence": "low",
                "note": "ANTHROPIC_API_KEY ikke satt."}
    try:
        small_bytes, media_type = _resize_image(image_bytes, content_type)
        img_b64 = base64.standard_b64encode(small_bytes).decode("utf-8")

        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            temperature=0,
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
                            "Du er ekspert på LEGO. Se på dette bildet og identifiser hva det viser.\n\n"
                            "Bildet kan vise ET AV DISSE:\n"
                            "- SET: en LEGO-eske, ferdig bygget sett eller uåpnet pakke\n"
                            "- MINIFIG: én eller flere minifigurer\n"
                            "- PART: én eller noen få løse LEGO-deler (klosser, plater, skinner etc.)\n"
                            "- BULK: en boks, pose eller haug med blandet LEGO\n"
                            "- INSTRUCTION: en instruksjonsbok/-hefte\n"
                            "- BOX: en tom LEGO-eske uten innhold\n"
                            "- GEAR: LEGO merchandise (klær, vesker, klokker etc.)\n"
                            "- CATALOG: LEGO-katalog\n"
                            "- MOC: tydelig egenbygd modell (ikke fra offisiell eske)\n"
                            "- OTHER: ukjent\n\n"
                            "Gjør disse vurderingene:\n"
                            "1. Hvilken type er dette? (type_guess)\n"
                            "2. SET/MINIFIG: identifiser settnummer hvis mulig.\n"
                            "3. PART: beskriv delen (form, studmønster, kategori) og en kort "
                            "søketerm egnet for Rebrickable (f.eks. '2x4 brick' eller 'curved slope 2x1').\n"
                            "4. Anslå år hvis relevant.\n"
                            "5. Slitasjegrad (kun for åpne/byggede objekter, null for forseglede).\n\n"
                            "VIKTIG — reissue-regel for SET: foretrekk nyeste utgave MED MINDRE "
                            "bildet viser vintage-eske eller vintage-klossfarger.\n\n"
                            "Svar KUN med JSON, ingen annen tekst:\n"
                            '{"type_guess":"SET","set_number":"75192","name":"Millennium Falcon",'
                            '"year":2017,"wear_level":"NEAR_MINT","wear_note":"Lett støv",'
                            '"part_description":null,"part_search_query":null,"confidence":"high"}\n\n'
                            "Felt-regler:\n"
                            "KRITISK DISTINKSJON — PART vs MOC:\n"
                            "- PART = én enkelt støpt LEGO-komponent, uansett hvor kompleks formen er "
                            "(f.eks. en tilhengerbunn, en minifig-torso, en buet plate, en spesialbrakett, "
                            "en baseplatte med hjul). Spør deg selv: 'Kom dette ut av én form?' → PART.\n"
                            "- MOC = noe et menneske har montert av FLERE deler. Ser du skjøter mellom "
                            "klosser, stud-til-tube-koblinger, eller deler i ulike farger satt sammen? → MOC.\n"
                            "Tvilstilfelle: velg PART.\n\n"
                            "type_guess: én av SET|MINIFIG|PART|BULK|INSTRUCTION|BOX|GEAR|CATALOG|MOC|OTHER\n"
                            "set_number: tall og bindestrek kun, f.eks. '75192' eller '71011-8', ellers null\n"
                            "part_description: norsk/engelsk beskrivelse av del-type og farge, null hvis ikke PART\n"
                            "part_search_query: 2-4 ord egnet for søk i Rebrickable (engelsk), null hvis ikke PART\n"
                            "confidence: 'high' hvis sikker, 'medium' hvis noenlunde, 'low' hvis usikker\n"
                            "wear_level: MINT|NEAR_MINT|VERY_GOOD|GOOD|FAIR eller null\n"
                            "wear_note: maks 60 tegn norsk eller null"
                        ),
                    },
                ],
            }],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        return {
            "type_guess":        result.get("type_guess", "OTHER"),
            "set_number":        result.get("set_number"),
            "name":              result.get("name"),
            "year":              result.get("year"),
            "wear_level":        result.get("wear_level"),
            "wear_note":         result.get("wear_note"),
            "part_description":  result.get("part_description"),
            "part_search_query": result.get("part_search_query"),
            "confidence":        result.get("confidence", "low"),
            "note":              "",
        }
    except json.JSONDecodeError:
        return {"type_guess": "OTHER", "set_number": None, "name": None, "year": None,
                "wear_level": None, "wear_note": None, "part_description": None,
                "part_search_query": None, "confidence": "low",
                "note": "Kunne ikke tolke svar fra AI."}
    except Exception as e:
        return {"type_guess": "OTHER", "set_number": None, "name": None, "year": None,
                "wear_level": None, "wear_note": None, "part_description": None,
                "part_search_query": None, "confidence": "low", "note": f"Feil: {e}"}

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


# ── Auth ──────────────────────────────────────────────────────────────────────

def _auth_client():
    return _sb_create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

def _pkce_pair():
    """Generer code_verifier og code_challenge for PKCE-flyten."""
    verifier = _secrets.token_urlsafe(64)
    challenge = _b64.urlsafe_b64encode(
        _hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge

# PKCE-flyt: vi sender code_verifier med i redirect_to-URL-en (?cv=...)
# slik at den kommer tilbake i callback sammen med code — ingen serverside state nødvendig.
_qp = st.query_params

if "code" in _qp and "user" not in st.session_state:
    _code = _qp["code"]
    _cv   = _qp.get("cv", "")
    if _cv:
        try:
            _session = _auth_client().auth.exchange_code_for_session({
                "auth_code":     _code,
                "code_verifier": _cv,
            })
            st.session_state["user"]         = _session.user
            st.session_state["access_token"] = _session.session.access_token
            st.query_params.clear()
            st.rerun()
        except Exception as _e:
            st.error(f"Innlogging feilet: {_e}")
            st.stop()
    else:
        st.error("Manglende code verifier — vennligst prøv å logge inn på nytt.")
        st.stop()

# Auth gate — vis innloggingsside hvis ikke autentisert
if "user" not in st.session_state:
    st.markdown("""
        <div style="display:flex;flex-direction:column;align-items:center;
                    justify-content:center;height:55vh;gap:16px;text-align:center">
            <h1>🧱 BrickHaus</h1>
            <p style="color:#888;font-size:1.1rem">Din LEGO-samling, organisert.</p>
        </div>
    """, unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if st.button("Logg inn med Google", use_container_width=True, type="primary"):
            _verifier, _challenge = _pkce_pair()
            _redirect = f"{APP_URL}?cv={_verifier}"
            _params = _urlparse.urlencode({
                "provider":              "google",
                "redirect_to":           _redirect,
                "code_challenge":        _challenge,
                "code_challenge_method": "S256",
                "scopes":                "email profile",
            })
            _auth_url = f"{SUPABASE_URL}/auth/v1/authorize?{_params}"
            st.markdown(
                f'<meta http-equiv="refresh" content="0;url={_auth_url}">',
                unsafe_allow_html=True,
            )
            st.stop()
    st.stop()

# Bruker er logget inn
_current_user = st.session_state["user"]


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

def sb_delete(table, filters: dict):
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**SB_HEADERS, "Prefer": "return=minimal"},
        params=filters,
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


@st.cache_data(ttl=3600, show_spinner=False)
def _bl_category_name(cat_id: int) -> tuple[str | None, int | None]:
    """Return (category_name, parent_id) for a BrickLink category, cached."""
    if not cat_id:
        return None, None
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/categories/{cat_id}",
            auth=_bl_auth(), timeout=8,
        )
        if not r.ok:
            return None, None
        data = r.json().get("data") or {}
        return data.get("category_name"), data.get("parent_id")
    except Exception:
        return None, None


@st.cache_data(ttl=3600, show_spinner=False)
def bl_fetch_set_metadata(set_number: str) -> dict | None:
    """
    Fetch BrickLink catalog metadata for a set — BL is authoritative
    (owned by LEGO). Returns dict with name, theme, subtheme, year.

    Category walk: BL stores sets in a single category_id, which may have
    a parent. We use the top-most ancestor as `theme` and the leaf (the
    set's direct category) as `subtheme` when different.
    """
    if not BL_CONSUMER_KEY or not set_number:
        return None
    bl_id = set_number if "-" in set_number else f"{set_number}-1"
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/SET/{bl_id}",
            auth=_bl_auth(), timeout=8,
        )
        if not r.ok:
            return None
        data = r.json().get("data") or {}
        name = html.unescape(data.get("name") or "") or None
        year = data.get("year_released")
        cat_id = data.get("category_id")

        # Walk category tree up to root
        chain: list[str] = []
        cur_id = cat_id
        seen = set()
        while cur_id and cur_id not in seen and len(chain) < 6:
            seen.add(cur_id)
            cat_name, parent_id = _bl_category_name(cur_id)
            if cat_name:
                chain.append(cat_name)
            cur_id = parent_id if parent_id and parent_id != cur_id else None

        # Strip BL meta-roots that aren't real themes — "Sets" sits above
        # all set categories as a catalog marker, not a theme.
        _BL_META_ROOTS = {"Sets", "Catalog", "Set"}
        while chain and chain[-1] in _BL_META_ROOTS:
            chain.pop()

        # chain = [leaf, ..., root]; theme = root, subtheme = leaf (if != root)
        theme    = chain[-1] if chain else None
        subtheme = chain[0] if len(chain) > 1 and chain[0] != chain[-1] else None

        return {
            "name":     name,
            "theme":    theme,
            "subtheme": subtheme,
            "year":     year,
        }
    except Exception:
        return None


_USD_TO_NOK = 10.5  # approximate fallback rate, updated periodically

def _bl_fetch_one(item_type: str, item_id: str,
                  new_or_used: str, guide_type: str) -> dict | None:
    """Try NOK/Europe, then USD global for a single condition (N or U)."""
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

    # Fallback: USD global → convert to NOK
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
        for key in ("min_price", "max_price", "avg_price", "qty_avg_price"):
            if data.get(key):
                data[key] = str(round(float(data[key]) * _USD_TO_NOK, 2))
        data["currency_code"] = "NOK"
        return data
    except Exception:
        return None

def _bl_fetch_raw(item_type: str, item_id: str,
                  condition: str, guide_type: str) -> dict | None:
    """BrickLink price lookup. Tries the matching condition first, then
    falls back to the opposite condition if no data exists."""
    new_or_used = "N" if condition == "SEALED" else "U"
    data = _bl_fetch_one(item_type, item_id, new_or_used, guide_type)
    if data:
        return data
    # Fallback: try opposite condition (e.g. "opened box" has no Used data,
    # but has New data — still a better estimate than nothing)
    opposite = "U" if new_or_used == "N" else "N"
    return _bl_fetch_one(item_type, item_id, opposite, guide_type)

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
            type_order = ["SET", "GEAR", "MINIFIG", "PART", "BOOK",
                          "CATALOG", "INSTRUCTION", "ORIGINAL_BOX"]  # numeric = probably set

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
    """Resolve theme/subtheme hierarchy and add _theme_name/_subtheme_name.

    Walks the FULL Rebrickable theme ancestry (not just one parent), so
    nested themes like Train > Promotional give both a theme and subtheme.
    chain is [leaf, parent, grandparent, ..., root]. We pick theme=root
    and subtheme=leaf when they differ.
    """
    theme_name, subtheme_name = "", ""
    theme_id = data.get("theme_id")
    if theme_id:
        chain: list[str] = []
        cur_id = theme_id
        seen: set[int] = set()
        while cur_id and cur_id not in seen and len(chain) < 6:
            seen.add(cur_id)
            try:
                tr = requests.get(
                    f"https://rebrickable.com/api/v3/lego/themes/{cur_id}/",
                    headers=RB_HEADERS, timeout=5,
                )
                if not tr.ok:
                    break
                td = tr.json()
            except Exception:
                break
            nm = (td.get("name") or "").strip()
            if nm:
                chain.append(nm)
            cur_id = td.get("parent_id")
        # Strip RB meta-roots that aren't real themes ("LEGO" sits above
        # all sets as a branding label and shouldn't be the theme).
        _RB_META_ROOTS = {"LEGO", "Lego"}
        while chain and chain[-1] in _RB_META_ROOTS:
            chain.pop()
        if chain:
            theme_name    = chain[-1]
            subtheme_name = chain[0] if len(chain) > 1 and chain[0] != chain[-1] else ""
            # Special case: if the chain collapsed to a single element and
            # that element is itself a known sub-category (e.g. "Promotional"),
            # expose it as subtheme instead of theme so BL can supply the
            # real parent theme on top.
            if len(chain) == 1 and chain[0] in {"Promotional", "Promotional Sets"}:
                theme_name    = ""
                subtheme_name = chain[0]
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

def rb_name_search(query: str, limit: int = 20) -> list:
    """Free-text name search against Rebrickable. Returns raw results
    without any base-number filtering, so callers can offer the user a
    pick-from-list experience when they typed a name instead of a number."""
    try:
        r = requests.get(
            "https://rebrickable.com/api/v3/lego/sets/",
            headers=RB_HEADERS,
            params={"search": query, "page_size": limit, "ordering": "-year"},
            timeout=10,
        )
        if not r.ok:
            return []
        return r.json().get("results", []) or []
    except Exception:
        return []


def _looks_like_set_number(q: str) -> bool:
    """Heuristic: '75192', '71011-8', '40370-1' → True; 'Millennium Falcon' → False."""
    q = q.strip()
    if not q:
        return False
    # Strict: digits, optionally followed by -digits (no spaces, no letters)
    import re
    return bool(re.fullmatch(r"\d+(?:-\d+)?", q))


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

def rb_minifig_count(set_number: str) -> int:
    """Fetch the number of minifigures in a set from Rebrickable."""
    try:
        r = requests.get(
            f"https://rebrickable.com/api/v3/lego/sets/{set_number}/minifigs/",
            headers=RB_HEADERS, timeout=8,
        )
        if not r.ok:
            return 0
        return r.json().get("count", 0)
    except Exception:
        return 0

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


def rb_search_parts(query: str, page_size: int = 12) -> list:
    """Search Rebrickable parts by name or part number.
    Returns list of {part_num, name, part_img_url, part_url}.
    """
    try:
        r = requests.get(
            "https://rebrickable.com/api/v3/lego/parts/",
            headers=RB_HEADERS,
            params={"search": query, "page_size": page_size},
            timeout=10,
        )
        if not r.ok:
            return []
        return r.json().get("results", [])
    except Exception:
        return []


@st.cache_data(ttl=86400)   # colors rarely change — cache 24 h
def rb_fetch_colors() -> list:
    """Fetch all Rebrickable colors. Returns list of {id, name, rgb, is_trans}."""
    try:
        results, url = [], "https://rebrickable.com/api/v3/lego/colors/?page_size=200"
        while url:
            r = requests.get(url, headers=RB_HEADERS, timeout=10)
            if not r.ok:
                break
            data = r.json()
            results.extend(data.get("results", []))
            url = data.get("next")
        return results
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
            params={"select": "id,ownership_id,object_type,set_number,bl_item_no,name,name_bl,theme,subtheme,year,condition,wear_level,status,location_id,sub_location,estimated_value_bl,total_cost_nok,quality_level,notes,insured,purchase_price,purchase_currency,purchase_date,purchase_source,registered_at,num_parts,num_minifigs,is_built,has_instructions,has_original_box,completeness_level,moc_base_set,instructions_url,instructions_storage_path,rebrickable_moc_id,parent_object_id,weight_kg,part_color_id,part_color_name"},
        )
        chunk = r.json()
        # Defensive: PostgREST returns a dict with {code, message, details, hint}
        # on error (e.g. missing column after an un-migrated schema change).
        # Iterating a dict would silently yield its keys as strings and
        # corrupt `objects`, so raise with a useful message instead.
        if isinstance(chunk, dict):
            raise RuntimeError(
                f"Supabase returned an error from /objects: "
                f"{chunk.get('message') or chunk}. "
                f"Har du kjørt alle migrasjoner i brickhaus/db/?"
            )
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
    # Filter only BH-* IDs. Mixed LG-* (Excel import) and BH-* (new) rows
    # would otherwise collide: lexicographic desc picks LG-xxx (L > B),
    # producing BH-{LG_num+1} every time → 409 on the unique constraint.
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/objects",
        headers=SB_HEADERS,
        params={
            "select":       "ownership_id",
            "ownership_id": "like.BH-*",
            "order":        "ownership_id.desc",
            "limit":        1,
        },
    )
    rows = r.json()
    if not rows:
        return "BH-0000001"
    last_id = rows[0]["ownership_id"]
    num = int(last_id.split("-")[1]) + 1
    return f"BH-{num:07d}"

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

# Per-unit slitasjegrad (BL-inspirert collector grading).
# Brukes for OPENED/BUILT/USED/INCOMPLETE — ikke meningsfullt for SEALED.
WEAR_LABEL = {
    "MINT":      "✨ Som ny",
    "NEAR_MINT": "🌟 Nesten som ny",
    "VERY_GOOD": "👍 Meget god",
    "GOOD":      "👌 God",
    "FAIR":      "🔧 Akseptabel",
}
# Conditions where wear_level matters (not sealed)
_WEAR_RELEVANT_CONDITIONS = {"OPENED", "BUILT", "USED", "INCOMPLETE"}
DOK_LABEL = {
    "BASIC":      "⚪ Registrert",
    "DOCUMENTED": "🔵 Dokumentert",
    "VERIFIED":   "🟢 Verifisert",
}

STATUS_LABEL = {
    "OWNED":  "I samlingen",
    "SOLD":   "Solgt",
    "LOANED": "Utlånt",
    "WANTED": "Ønskeliste",
}

# "Mangler noe?" framing — flipped from the old clinical "Kompletthetsgrad"
# so a non-technical user can answer a natural question instead of grading
# a completeness level.
COMPLETENESS_LABEL = {
    "COMPLETE":         "Komplett",
    "NEARLY_COMPLETE":  "Noen få deler mangler",
    "INCOMPLETE":       "Mye mangler",
    "UNKNOWN":          "Vet ikke",
}
TYPE_LABEL = {
    "SET":            "Sett",
    "MINIFIG":        "Minifig",
    "PART":           "Del",
    "GEAR":           "Gear",
    "BOOK":           "Bok",
    "CATALOG":        "Katalog",
    "INSTRUCTION":    "Instruksjon",
    "ORIGINAL_BOX":   "Originalboks",
    "MOC":            "MOC",
    "MOD":            "Mod",
    "BULK":           "Bulk",
    "BULK_CONTAINER": "Bulk",   # legacy — migrated to BULK
}

def fmt_nok(val):
    if val is None:
        return "–"
    return f"{int(val):,} kr".replace(",", "\u00a0")


def fmt_bh_id(oid: str | None) -> tuple[str, str]:
    """Return (compact, full) form of a BH-ID.

    Compact: '#585' for readability, with a thin-space thousands separator
    once the numeric part crosses 1 000 ('#1 234', '#12 345').
    Full:    the canonical 'BH-0000585' form, preserved for delete guards
             and copy-paste workflows.
    """
    if not oid:
        return "–", "–"
    if not oid.startswith("BH-"):
        return oid, oid
    try:
        num = int(oid[3:])
    except ValueError:
        return oid, oid
    compact = f"#{num:,}".replace(",", "\u00a0")
    return compact, oid


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
        "rb_name":          "",
        "rb_theme":         "",
        "rb_subtheme":      "",
        "rb_year":          date.today().year,
        "rb_img":           None,
        "rb_parts":         None,
        "rb_minifigs":      None,
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
        "last_shown_oid":    None,
        "pending_edit_oid":  None,
        "reg_uploaded_img_bytes": None,  # cached bytes from AI flow, reused as documentation
        "reg_uploaded_img_type":  None,
        "reg_doc_img_saved":  False,     # tracks whether documentation image was saved in step 5
        "reg_last_img_file_id":  None,    # prevents re-running identification on rerun
        "reg_condition":        None,    # segmented control state for Tilstand
        "reg_wear_level":       None,    # segmented control state for slitasje
        "reg_has_instructions": False,
        "reg_has_original_box": False,
        "reg_completeness":     "UNKNOWN",
        "bl_name_secondary":    None,    # BL official name shown as caption when != name
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

def reset_registration():
    keys = ["rb_name","rb_theme","rb_subtheme","rb_img","rb_parts","rb_minifigs","rb_status",
            "reg_set_number","pending_record","confirm_no_loc","rb_fetch_trigger","rb_variants",
            "reg_saved","reg_ownership_id","reg_obj_uuid",
            "reg_input_mode","reg_ai_result","moc_prefill","moc_rb_results",
            "reg_uploaded_img_bytes","reg_uploaded_img_type",
            "reg_last_img_file_id","reg_condition","reg_wear_level",
            "bl_name_secondary",
            # Part flow
            "reg_part_result","reg_part_search_results","reg_part_color_id",
            "reg_part_color_name","reg_part_qty","reg_part_ai_triggered",
            # Bulk flow
            "reg_bulk_name","reg_bulk_notes","reg_bulk_weight",
            # MOD flow
            "reg_mod_parent_id","reg_mod_parent_name","reg_mod_search_results",
            "reg_mod_base_set_text",
            ]
    for k in keys:
        st.session_state[k] = None
    st.session_state["rb_year"]  = date.today().year
    st.session_state["rb_status"] = None
    st.session_state["reg_has_instructions"] = False
    st.session_state["reg_has_original_box"] = False
    st.session_state["reg_completeness"]     = "UNKNOWN"
    st.session_state["reg_step"] = 1
    st.session_state["reg_doc_img_saved"] = False


# ── Edit dialog ───────────────────────────────────────────────────────────────

@st.dialog("Objekt", width="large")
def detail_dialog(obj: dict, loc_list: list):
    """Read-only detail view of an object. Clicking Rediger flips the
    session into edit mode — on the next rerun the collection view will
    open edit_dialog for the same object.
    """
    oid = obj.get("ownership_id", "")
    compact_id, full_id = fmt_bh_id(oid)

    # Header: big compact ID, muted full BH-ID underneath for copy-paste/delete guard.
    st.markdown(
        f"### {compact_id}  "
        f"<span style='color:#888;font-weight:normal;font-size:0.7em'>"
        f"({full_id})</span>",
        unsafe_allow_html=True,
    )

    # ── Top: image + key facts ───────────────────────────────────────────────
    col_img, col_info = st.columns([1, 2])
    with col_img:
        obj_uuid = obj.get("id")
        img_shown = False
        if obj_uuid:
            try:
                existing_img = fetch_object_image(obj_uuid)
            except Exception:
                existing_img = None
            if existing_img:
                st.image(image_public_url(existing_img["storage_path"]),
                         use_container_width=True)
                img_shown = True
        if not img_shown:
            st.caption("– Ingen bilde –")

    with col_info:
        disp_name = display_name(obj) or "–"
        st.markdown(f"#### {html.escape(disp_name)}")
        bl_full = obj.get("name_bl")
        if bl_full:
            st.caption(f"🏷️ BrickLink: {html.unescape(bl_full)}")
        sn = obj.get("set_number") or "–"
        bl_no = obj.get("bl_item_no") or ""
        id_line = f"📦 Settnr: **{sn}**"
        if bl_no and bl_no != sn:
            id_line += f"  ·  BL Item: **{bl_no}**"
        st.caption(id_line)

        status_lbl = STATUS_LABEL.get(obj.get("status", "OWNED"), "–")
        cond_lbl   = CONDITION_LABEL.get(obj.get("condition", ""), "–")
        st.markdown(f"**Status:** {status_lbl} &nbsp;·&nbsp; **Tilstand:** {cond_lbl}",
                    unsafe_allow_html=True)

        val = obj.get("estimated_value_bl")
        if val:
            st.markdown(f"💰 **Verdi:** {fmt_nok(val)}")

    st.divider()

    # ── Facts list (two compact columns) ─────────────────────────────────────
    def _kv(label: str, value):
        if value in (None, "", "–"):
            value = "–"
        return (
            f"<div style='display:flex;gap:10px;padding:3px 0'>"
            f"<div style='width:140px;color:#666'>{label}</div>"
            f"<div style='color:#111'>{html.escape(str(value))}</div></div>"
        )

    left_rows  = [
        _kv("Type",          TYPE_LABEL.get(obj.get("object_type") or "", "–")),
        _kv("Tema",          obj.get("theme") or "–"),
        _kv("Subtema",       obj.get("subtheme") or "–"),
        _kv("År",            obj.get("year") or "–"),
        _kv("Deler",         obj.get("num_parts") or "–"),
        _kv("Minifigurer",   obj.get("num_minifigs") or "–"),
    ]
    _wear = obj.get("wear_level")
    _mangler = COMPLETENESS_LABEL.get(obj.get("completeness_level") or "UNKNOWN", "–")
    right_rows = [
        _kv("Bruksspor",      WEAR_LABEL.get(_wear, "–") if _wear else "–"),
        _kv("Instruksjoner",  "Ja" if obj.get("has_instructions") else "Nei"),
        _kv("Original boks",  "Ja" if obj.get("has_original_box") else "Nei"),
        _kv("Mangler noe?",   _mangler),
        _kv("Lokasjon",       obj.get("location_name") or "–"),
        _kv("Sub-lokasjon",   obj.get("sub_location") or "–"),
    ]
    c_l, c_r = st.columns(2)
    with c_l:
        st.markdown("".join(left_rows), unsafe_allow_html=True)
    with c_r:
        st.markdown("".join(right_rows), unsafe_allow_html=True)

    if obj.get("notes"):
        st.markdown("**Notater**")
        st.caption(obj["notes"])

    # Purchase info (only if present)
    if obj.get("purchase_price") or obj.get("purchase_date") or obj.get("purchase_source"):
        st.divider()
        st.markdown("**Kjøpt**")
        parts = []
        if obj.get("purchase_price"):
            cur = obj.get("purchase_currency") or "NOK"
            parts.append(f"{int(obj['purchase_price'])} {cur}")
        if obj.get("purchase_date"):
            parts.append(obj["purchase_date"])
        if obj.get("purchase_source"):
            parts.append(obj["purchase_source"])
        st.caption(" · ".join(parts))

    if obj.get("registered_at"):
        st.caption(f"📅 Registrert: {obj['registered_at']}")

    st.divider()
    _is_set = obj.get("object_type") == "SET"
    if _is_set:
        col_edit, col_mod, col_close = st.columns([2, 2, 1])
    else:
        col_edit, col_close = st.columns([2, 1])
    with col_edit:
        if st.button("✏️ Rediger", type="primary", use_container_width=True,
                     key=f"detail_edit_{oid}"):
            st.session_state["pending_edit_oid"] = oid
            st.session_state["last_shown_oid"]   = None
            st.rerun()
    if _is_set:
        with col_mod:
            if st.button("🔧 Legg til MOD", use_container_width=True,
                         key=f"detail_add_mod_{oid}"):
                reset_registration()
                st.session_state["reg_input_mode"]     = "mod"
                st.session_state["reg_mod_parent_id"]  = obj.get("id")
                st.session_state["reg_mod_parent_name"] = (
                    f"{obj.get('set_number','?')} – {display_name(obj)}"
                )
                st.session_state["last_shown_oid"] = None
                st.rerun()
    with col_close:
        if st.button("Lukk", use_container_width=True, key=f"detail_close_{oid}"):
            st.session_state["last_shown_oid"] = None
            st.rerun()


@st.dialog("Rediger objekt", width="large")
def edit_dialog(obj: dict, loc_list: list):
    oid = obj.get("ownership_id", "")
    compact_id, full_id = fmt_bh_id(oid)
    # Big, readable compact ID. Full BH-ID lives one line below as a muted
    # reference (needed for the delete guard, copy-paste into support etc.).
    st.markdown(
        f"### {compact_id}  "
        f"<span style='color:#888;font-weight:normal;font-size:0.75em'>"
        f"({full_id})</span>",
        unsafe_allow_html=True,
    )
    st.caption(f"Registrert: {obj.get('registered_at', '–')}")

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

    # ── Identitet ────────────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        is_moc = obj.get("object_type") in ("MOC", "MOD")
        if is_moc:
            name = st.text_input("Navn *", value=obj.get("name") or "")
        else:
            # Read-only visual rendering with good contrast — a disabled
            # text_input is hard to read, so we show the name as a labelled
            # markdown block and stash the value for the save path.
            name_val = display_name(obj) or "–"
            st.markdown("**Navn**")
            st.markdown(
                f"<div style='padding:6px 10px;border:1px solid #d0d0d0;"
                f"border-radius:6px;background:#f6f7fb;color:#111;"
                f"font-weight:500'>{html.escape(name_val)}</div>",
                unsafe_allow_html=True,
            )
            st.caption("Navn hentes fra BrickLink / Rebrickable og kan kun redigeres for MOC og MOD.")
            name = obj.get("name") or ""
        set_number_edit = st.text_input("Settnummer", value=obj.get("set_number") or "",
                                        help="Ditt logiske settnummer, f.eks. 71011-16 for Queen i Series 15.")
        bl_item_no_edit = st.text_input("BrickLink Item-nr",
                                         value=obj.get("bl_item_no") or "",
                                         help="BrickLinks eget oppslags-ID for pris og navn. "
                                              "F.eks. col15-16 for CMF, eller 6385680-1 for komplett boks. "
                                              "Brukes til pris/navn-oppslag hvis ulikt settnummer.")
        # Filter out legacy BULK_CONTAINER from type selector
        type_keys = [k for k in TYPE_LABEL.keys() if k != "BULK_CONTAINER"]
        cur_type = obj.get("object_type", "SET")
        if cur_type == "BULK_CONTAINER":
            cur_type = "BULK"
        object_type = st.selectbox("Type", type_keys,
                                   index=type_keys.index(cur_type) if cur_type in type_keys else 0,
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

        # Bruksspor (per-unit grading) — only meaningful for non-sealed
        if condition in _WEAR_RELEVANT_CONDITIONS:
            wear_keys = ["(ingen)"] + list(WEAR_LABEL.keys())
            cur_wear  = obj.get("wear_level")
            wear_idx  = wear_keys.index(cur_wear) if cur_wear in wear_keys else 0
            wear_pick = st.selectbox(
                "Bruksspor",
                wear_keys,
                index=wear_idx,
                format_func=lambda x: WEAR_LABEL[x] if x in WEAR_LABEL else "– Ikke satt –",
                help="Hvor slitt ditt eksemplar er — fra 'Som ny' til 'Akseptabel'.",
            )
            wear_level_val = wear_pick if wear_pick in WEAR_LABEL else None
        else:
            wear_level_val = None

        status_keys = list(STATUS_LABEL.keys())
        cur_status = obj.get("status", "OWNED")
        status_idx = status_keys.index(cur_status) if cur_status in status_keys else 0
        obj_status = st.selectbox("Status", status_keys,
                                   index=status_idx,
                                   format_func=lambda x: STATUS_LABEL[x])
        loc_names   = [l["name"] for l in loc_list]
        current_loc = obj.get("location_name") or "– Ingen –"
        loc_options = ["– Ingen –"] + loc_names
        loc_idx     = loc_options.index(current_loc) if current_loc in loc_options else 0
        location    = st.selectbox("Lokasjon", loc_options, index=loc_idx)
        new_loc     = st.text_input("Eller ny lokasjon", placeholder="f.eks. Loft")
        sub_loc     = st.text_input("Sub-lokasjon", value=obj.get("sub_location") or "")
        notes       = st.text_area("Notater", value=obj.get("notes") or "")

    # ── Innhold & komplettering ──────────────────────────────────────────────
    st.subheader("Innhold")
    ic1, ic2, ic3 = st.columns(3)
    with ic1:
        num_parts = st.number_input("Antall deler", min_value=0, step=1,
                                     value=int(obj.get("num_parts") or 0))
        num_minifigs = st.number_input("Antall minifigurer", min_value=0, step=1,
                                        value=int(obj.get("num_minifigs") or 0))
    with ic2:
        has_instructions = st.toggle("Har instruksjoner",
                                      value=bool(obj.get("has_instructions")),
                                      key=f"has_instr_{oid}")
        has_original_box = st.toggle("Har original boks",
                                      value=bool(obj.get("has_original_box")),
                                      key=f"has_box_{oid}")
    with ic3:
        compl_keys = list(COMPLETENESS_LABEL.keys())
        cur_compl = obj.get("completeness_level") or "UNKNOWN"
        if cur_compl not in compl_keys:
            cur_compl = "UNKNOWN"
        completeness = st.selectbox("Mangler noe?", compl_keys,
                                     index=compl_keys.index(cur_compl),
                                     format_func=lambda x: COMPLETENESS_LABEL[x],
                                     help="Mangler det deler, instruksjoner eller minifigurer?")

    # ── Documentation image ───────────────────────────────────────────────────
    st.subheader("📷 Bilde")
    obj_uuid = obj.get("id")
    if obj_uuid:
        existing_img = fetch_object_image(obj_uuid)
        if existing_img:
            st.image(image_public_url(existing_img["storage_path"]), width=280)
        else:
            st.caption("Ingen bilde lastet opp ennå.")
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
                    st.success("📷 Bilde lagret!")
                    st.rerun()
                else:
                    st.error("Opplasting feilet. Sjekk at storage-bucket er opprettet i Supabase.")

    st.subheader("Verdi")
    est_value = st.number_input(
        "Estimert verdi (NOK)",
        value=float(obj.get("estimated_value_bl") or 0),
        min_value=0.0, step=10.0,
        help="Vektet gjennomsnittspris fra BrickLink — basert på faktiske salg "
             "og aktive annonser i tilsvarende tilstand. Oppdateres automatisk "
             "hver måned. Du kan overstyre manuelt ved behov.",
    )

    # ── MOC / MOD-spesifikke felt ─────────────────────────────────────────────
    if object_type in ("MOC", "MOD"):
        st.subheader("🔧 MOC / MOD")
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            moc_base_set = st.text_input(
                "Basert på sett (kun MOD)",
                value=obj.get("moc_base_set") or "",
                placeholder="f.eks. 75192-1 eller BH-0000042",
                disabled=object_type != "MOD",
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
    with col_del:
        if st.button("🗑️ Slett", use_container_width=True, key=f"del_btn_{oid}"):
            st.session_state[f"confirm_del_{oid}"] = True
            st.rerun()
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
                "wear_level":        wear_level_val,
                "status":            obj_status,
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
                "num_minifigs":         int(num_minifigs) if num_minifigs else None,
                "has_instructions":     has_instructions,
                "has_original_box":     has_original_box,
                "is_built":             condition in ("BUILT", "USED"),
                "completeness_level":   completeness,
                "moc_base_set":         moc_base_set.strip() if moc_base_set else None,
                "rebrickable_moc_id":   rebrickable_moc_id.strip() if rebrickable_moc_id else None,
                "instructions_url":     instructions_url.strip() if instructions_url else None,
            }
            sb_patch("objects", {"ownership_id": f"eq.{oid}"}, updates)

            # If set_number or bl_item_no changed, immediately refetch BL name and price
            if sn_changed or bl_changed:
                with st.spinner("Henter oppdatert BL-navn og pris ..."):
                    try:
                        new_price, new_bl_name = bl_get_price(
                            set_number=new_sn or obj.get("set_number") or "",
                            condition=condition,
                            object_type=object_type,
                            name=name or obj.get("name") or "",
                            bl_item_no=new_bl or "",
                        )
                    except Exception as _e:
                        new_price, new_bl_name = None, None
                        st.warning(f"BL-oppslag feilet: {_e}")
                sb_patch("objects", {"ownership_id": f"eq.{oid}"}, {
                    "name_bl": new_bl_name,
                    "estimated_value_bl": new_price,
                })
                if new_bl_name or new_price:
                    st.toast(f"BL oppdatert: {new_bl_name or '–'} · {fmt_nok(new_price) if new_price else '–'}", icon="✅")
                else:
                    st.toast("Fant ikke BL-treff for nytt settnummer/BL-ID", icon="⚠️")

            # Upload new instruction file if provided
            if new_instr_file:
                path = upload_instructions_file(oid, new_instr_file.read(), new_instr_file.type)
                if path:
                    sb_patch("objects", {"ownership_id": f"eq.{oid}"},
                             {"instructions_storage_path": path})

            st.cache_data.clear()
            st.rerun()

    # ── Delete confirmation panel ─────────────────────────────────────────────
    if st.session_state.get(f"confirm_del_{oid}"):
        st.divider()
        st.error(
            "⚠️ **Du er i ferd med å slette dette objektet permanent.**\n\n"
            "Sletting kan IKKE angres. All historikk forsvinner — inkludert status "
            "som SOLGT, UTLÅNT, notater, kjøpshistorikk og dokumentasjonsbilde. "
            "Hvis objektet er solgt og du ønsker å bevare historikken, sett Status "
            "til *Solgt* i stedet for å slette.\n\n"
            f"For å bekrefte, skriv inn objektets ID **{oid}** nedenfor:"
        )
        confirm_txt = st.text_input(
            "Bekreftelses-ID",
            key=f"confirm_txt_{oid}",
            placeholder=oid,
            label_visibility="collapsed",
        )
        cc1, cc2 = st.columns(2)
        with cc1:
            if st.button("← Avbryt", use_container_width=True, key=f"cancel_del_{oid}"):
                st.session_state[f"confirm_del_{oid}"] = False
                st.session_state.pop(f"confirm_txt_{oid}", None)
                st.rerun()
        with cc2:
            can_delete = confirm_txt.strip() == oid
            if st.button("🗑️ Slett permanent", type="primary",
                         disabled=not can_delete,
                         use_container_width=True,
                         key=f"do_del_{oid}"):
                try:
                    sb_delete("objects", {"ownership_id": f"eq.{oid}"})
                    st.session_state[f"confirm_del_{oid}"] = False
                    st.session_state.pop(f"confirm_txt_{oid}", None)
                    st.cache_data.clear()
                    st.toast(f"Slettet {oid}", icon="🗑️")
                    st.rerun()
                except Exception as _e:
                    st.error(f"Sletting feilet: {_e}")


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
        search = st.text_input("🔍 Søk", "", key="filter_search")

        all_themes = sorted({o["theme"] for o in objects if o.get("theme")})
        sel_themes = st.multiselect("Tema", all_themes, key="filter_themes")

        all_types = sorted({o["object_type"] for o in objects if o.get("object_type")})
        sel_types = st.multiselect("Type", all_types, format_func=lambda x: TYPE_LABEL.get(x, x), key="filter_types")

        all_conds = sorted({o["condition"] for o in objects if o.get("condition")})
        sel_conds = st.multiselect("Tilstand", all_conds, format_func=lambda x: CONDITION_LABEL.get(x, x), key="filter_conds")

        all_locs = sorted({o["location_name"] for o in objects if o.get("location_name") != "–"})
        sel_locs = st.multiselect("Lokasjon", all_locs, key="filter_locs")

        has_filter = bool(search or sel_themes or sel_types or sel_conds or sel_locs)
        if has_filter:
            if st.button("✕ Nullstill filter", use_container_width=True):
                for k in ("filter_search", "filter_themes", "filter_types",
                           "filter_conds", "filter_locs"):
                    st.session_state[k] = [] if k != "filter_search" else ""
                st.rerun()

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

    n_objects   = len(filtered)
    total_value = sum(o.get("estimated_value_bl") or 0 for o in filtered)

    c1, c2 = st.columns(2)
    c1.metric("Antall objekter", f"{n_objects}")
    c2.metric("Estimert verdi", fmt_nok(total_value) if total_value else "–")

    st.divider()

    if not filtered:
        st.info("Ingen objekter matcher filteret.")
    else:
        # Group selector lives above the table (sidebar is hidden by default on mobile)
        group_options = {"Ingen": None, "Tema": "theme", "Type": "object_type",
                         "Lokasjon": "location_name", "År": "year"}
        gcol1, gcol2 = st.columns([1, 3])
        with gcol1:
            group_by = st.selectbox("Grupper etter", list(group_options.keys()),
                                    index=list(group_options.keys()).index(
                                        st.session_state.get("group_by_select", "Ingen")),
                                    key="group_by_select")

        # Pre-compute status for display (and unused sort fallback)
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

        # ── Natural sort on set_number ──────────────────────────────────────
        # Split "71011-16" into (71011, 16) so that 8683-2 < 8683-10 < 8684-0
        # and e.g. 8684-0 always follows the full 8683-X block. Plain lex sort
        # gave weird interleaving; numeric-aware sort is stable and predictable.
        def _natural_set_key(o):
            sn = (o.get("set_number") or "").strip()
            if not sn:
                return (10**9, 10**9)
            base, _, suf = sn.partition("-")
            try:
                b = int(base)
            except ValueError:
                # Non-numeric base (e.g. "col15-16" or MOC) — push to the end
                # but keep internal ordering stable
                return (10**9, sn)
            try:
                s = int(suf) if suf else 0
            except ValueError:
                s = 0
            return (b, s)
        filtered.sort(key=_natural_set_key)

        st.caption("Klikk en kolonne for å sortere, eller en rad for å redigere.")

        def _make_rows(objs):
            return [{
                "Status":   _row_status(o),
                "Settnr.":  o.get("set_number") or "–",
                "Navn":     display_name(o),
                "Tema":     o.get("theme") or "–",
                "År":       o.get("year") or "–",
                "Type":     TYPE_LABEL.get(o.get("object_type", ""), ""),
                "Tilstand": CONDITION_LABEL.get(o.get("condition", ""), "–"),
                "Verdi":    fmt_nok(o.get("estimated_value_bl")),
                "Lokasjon": o.get("location_name", "–"),
                "ID":       fmt_bh_id(o.get("ownership_id", ""))[0],
            } for o in objs]

        _col_config = {
            "Status": st.column_config.TextColumn(width="small"),
            "Type":   st.column_config.TextColumn(width="small"),
            "År":     st.column_config.TextColumn(width="small"),
            "ID":     st.column_config.TextColumn(width="small"),
        }

        group_field = group_options.get(group_by)

        if not group_field:
            # ── Flat table (no grouping) ─────────────────────────────────
            rows = _make_rows(filtered)
            event = st.dataframe(
                rows,
                use_container_width=True,
                height=600,
                on_select="rerun",
                selection_mode="single-row",
                column_config=_col_config,
            )
            selected = event.selection.rows
            if selected:
                obj = filtered[selected[0]]
                obj["location_name"] = loc_by_id.get(obj.get("location_id"), "– Ingen –")
                _oid = obj.get("ownership_id")
                if st.session_state.get("pending_edit_oid") == _oid:
                    st.session_state["pending_edit_oid"] = None
                    st.session_state["last_shown_oid"]   = _oid
                    edit_dialog(obj, loc_list)
                elif st.session_state.get("last_shown_oid") != _oid:
                    st.session_state["last_shown_oid"] = _oid
                    detail_dialog(obj, loc_list)
            else:
                st.session_state["last_shown_oid"] = None
        else:
            # ── Accordion grouping ───────────────────────────────────────
            from collections import OrderedDict
            _any_selected = False
            groups = OrderedDict()
            for o in filtered:
                key = o.get(group_field) or "– Ukjent –"
                if group_field == "object_type":
                    key = TYPE_LABEL.get(key, key)
                elif group_field == "year":
                    key = str(key) if key else "– Ukjent –"
                groups.setdefault(key, []).append(o)

            for grp_name, grp_objs in sorted(groups.items()):
                grp_value = sum(o.get("estimated_value_bl") or 0 for o in grp_objs)
                grp_label = f"{grp_name} ({len(grp_objs)} obj."
                if grp_value:
                    grp_label += f" · {fmt_nok(grp_value)}"
                grp_label += ")"

                with st.expander(grp_label):
                    # Sub-group by subtema if grouping by tema
                    if group_field == "theme":
                        sub_groups = OrderedDict()
                        for o in grp_objs:
                            sk = o.get("subtheme") or "– Generelt –"
                            sub_groups.setdefault(sk, []).append(o)
                        if len(sub_groups) > 1:
                            for sub_name, sub_objs in sorted(sub_groups.items()):
                                sub_val = sum(o.get("estimated_value_bl") or 0 for o in sub_objs)
                                st.caption(f"**{sub_name}** — {len(sub_objs)} obj."
                                           + (f" · {fmt_nok(sub_val)}" if sub_val else ""))

                    rows = _make_rows(grp_objs)
                    event = st.dataframe(
                        rows,
                        use_container_width=True,
                        height=min(400, 35 * len(grp_objs) + 40),
                        on_select="rerun",
                        selection_mode="single-row",
                        column_config=_col_config,
                        key=f"grp_{grp_name}",
                    )
                    selected = event.selection.rows
                    if selected:
                        _any_selected = True
                        obj = grp_objs[selected[0]]
                        obj["location_name"] = loc_by_id.get(obj.get("location_id"), "– Ingen –")
                        _oid = obj.get("ownership_id")
                        if st.session_state.get("pending_edit_oid") == _oid:
                            st.session_state["pending_edit_oid"] = None
                            st.session_state["last_shown_oid"]   = _oid
                            edit_dialog(obj, loc_list)
                        elif st.session_state.get("last_shown_oid") != _oid:
                            st.session_state["last_shown_oid"] = _oid
                            detail_dialog(obj, loc_list)

            if not _any_selected:
                st.session_state["last_shown_oid"] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TAB 2: REGISTRER — mobile-first, step-by-step
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

with tab_register:

    step = st.session_state["reg_step"]

    # Persistent context header — shows current set number and name
    # throughout all registration steps once they are known.
    _ctx_sn   = st.session_state.get("reg_set_number")
    _ctx_name = st.session_state.get("rb_name")
    if _ctx_sn:
        _bits = [f"📦 **{_ctx_sn}**"]
        if _ctx_name:
            _bits.append(_ctx_name)
        st.caption(" · ".join(_bits))

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

    # ── Global Avbryt — shown in all steps once a flow is active ─────────────
    _in_reg_flow = (step > 1 or st.session_state.get("reg_input_mode") is not None)
    if _in_reg_flow:
        if st.button("✕ Avbryt registrering", key="reg_cancel_global_top"):
            reset_registration()
            st.rerun()

    # ── STEP 1: Choose input mode + identify ─────────────────────────────────
    if step == 1:

        def _apply_rb_data(data: dict):
            st.session_state["rb_name"]     = data.get("name", "")
            st.session_state["rb_theme"]    = data.get("_theme_name", "")
            st.session_state["rb_subtheme"] = data.get("_subtheme_name", "")
            st.session_state["rb_year"]     = data.get("year", date.today().year)
            st.session_state["rb_img"]      = data.get("set_img_url")
            st.session_state["rb_parts"]    = data.get("num_parts")
            st.session_state["rb_minifigs"] = rb_minifig_count(data.get("set_num", ""))
            st.session_state["rb_status"]   = "found"
            st.session_state["rb_variants"] = None

            # Overlay BrickLink on top of Rebrickable. BL is authoritative
            # for theme/subtheme (BL is owned by LEGO and the catalog
            # hierarchy is the source of truth). For NAME we keep RB as
            # primary (collectors recognise "40 Years of LEGO Trains" more
            # than BL's "Steam Engine {Reissue of Set 7810}") and store
            # the BL official name as a secondary caption.
            bl_meta = bl_fetch_set_metadata(data.get("set_num", ""))
            st.session_state["bl_name_secondary"] = None
            if bl_meta:
                bl_n = (bl_meta.get("name") or "").strip()
                rb_n = (st.session_state.get("rb_name") or "").strip()
                if bl_n and bl_n != rb_n:
                    st.session_state["bl_name_secondary"] = bl_n
                if bl_meta.get("theme"):
                    st.session_state["rb_theme"] = bl_meta["theme"]
                if bl_meta.get("subtheme"):
                    st.session_state["rb_subtheme"] = bl_meta["subtheme"]
                if bl_meta.get("year"):
                    st.session_state["rb_year"] = bl_meta["year"]

            st.session_state["reg_step"]    = 2

        def _do_rb_fetch(raw: str):
            q = raw.strip()
            if _looks_like_set_number(q):
                # Traditional set-number flow: find all variants sharing
                # the base number and let the user pick if there are several.
                base = q.split("-")[0]
                variants = rb_search_variants(base)
            else:
                # Name search: hit the database with free text and
                # present any matches back as a pick-from-list.
                variants = rb_name_search(q)

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

            # If user came from photo flow and chose "Velg type manuelt",
            # the image bytes are still in session state — show a notice so
            # they know the image is carried forward.
            _cached_img = st.session_state.get("reg_uploaded_img_bytes")
            if _cached_img:
                _ci, _ct = st.columns([1, 4])
                with _ci:
                    st.image(_cached_img, width=60)
                with _ct:
                    st.caption("📷 Bilde er klar — velger du **Deler**, **Bulk**, **MOC** eller **MOD** "
                               "blir bildet lagret automatisk med oppføringen.")

            # Top row: two primary entry points
            top1, top2 = st.columns(2)
            with top1:
                if st.button("📷  Gjenkjenn fra foto", use_container_width=True,
                             type="primary", disabled=not ANTHROPIC_API_KEY):
                    st.session_state["reg_input_mode"] = "image"
                    st.rerun()
                st.caption("Alle typer" if ANTHROPIC_API_KEY else "Krever ANTHROPIC_API_KEY")
            with top2:
                if st.button("🔢  Søk på nummer", use_container_width=True, type="primary"):
                    st.session_state["reg_input_mode"] = "number"
                    st.rerun()
                st.caption("Sett, minifig, del")

            st.write("")

            # Bottom row: four manual entry points
            b1, b2, b3, b4 = st.columns(4)
            with b1:
                if st.button("🧩  Deler", use_container_width=True):
                    st.session_state["reg_input_mode"] = "part"
                    st.rerun()
                st.caption("Løse")
            with b2:
                if st.button("📦  Bulk", use_container_width=True):
                    st.session_state["reg_input_mode"] = "bulk"
                    st.rerun()
                st.caption("Blanding")
            with b3:
                if st.button("🏗️  MOC", use_container_width=True):
                    st.session_state["reg_input_mode"] = "moc"
                    st.rerun()
                st.caption("Eget bygg")
            with b4:
                if st.button("🔧  MOD", use_container_width=True):
                    st.session_state["reg_input_mode"] = "mod"
                    st.rerun()
                st.caption("Modifisert")

            st.divider()
            _progress_indicator(step)

        # ── Image path ────────────────────────────────────────────────────────
        elif input_mode == "image":
            ai_result = st.session_state.get("reg_ai_result")

            if ai_result is None:
                st.subheader("📷 Gjenkjenn fra foto")
                st.caption("Last opp bilde av hva som helst — sett, minifig, del, bulk, eske …")
                img_file = st.file_uploader(
                    "Velg bilde",
                    type=["jpg", "jpeg", "png", "webp"],
                    key="reg_id_img",
                    label_visibility="collapsed",
                )
                if img_file is not None:
                    file_id = getattr(img_file, "file_id", None) or img_file.name
                    if st.session_state.get("reg_last_img_file_id") != file_id:
                        st.session_state["reg_last_img_file_id"] = file_id
                        img_bytes = img_file.read()
                        # Apply EXIF rotation correction immediately so every
                        # downstream display and AI call sees the right orientation.
                        try:
                            from PIL import ImageOps as _IOP
                            _im = Image.open(io.BytesIO(img_bytes))
                            _im = _IOP.exif_transpose(_im)
                            _buf = io.BytesIO()
                            _fmt = "JPEG" if "jpeg" in img_file.type or "jpg" in img_file.type else "PNG"
                            _im.convert("RGB").save(_buf, format=_fmt, quality=90)
                            img_bytes = _buf.getvalue()
                        except Exception:
                            pass
                        st.session_state["reg_uploaded_img_bytes"] = img_bytes
                        st.session_state["reg_uploaded_img_type"]  = img_file.type
                        with st.spinner("Analyserer bilde ..."):
                            result = identify_lego_from_image(img_bytes, img_file.type)
                        st.session_state["reg_ai_result"] = result
                        ai_wear = result.get("wear_level")
                        if ai_wear in WEAR_LABEL:
                            st.session_state["reg_wear_level"] = ai_wear
                        # Pre-fetch RB reference image for SET/MINIFIG
                        sett = result.get("set_number")
                        if sett and result.get("type_guess") in ("SET", "MINIFIG", None):
                            try:
                                variants = rb_search_variants(sett.split("-")[0])
                                if variants:
                                    match = next((v for v in variants
                                                  if v.get("set_num") == sett), variants[0])
                                    result["_rb_img"]     = match.get("set_img_url")
                                    result["_rb_name"]    = match.get("name")
                                    result["_rb_set_num"] = match.get("set_num")
                            except Exception:
                                pass
                        # Pre-fetch part candidates for PART type
                        if result.get("type_guess") == "PART" and result.get("part_search_query"):
                            try:
                                result["_part_candidates"] = rb_search_parts(
                                    result["part_search_query"], page_size=8)
                            except Exception:
                                result["_part_candidates"] = []
                        st.rerun()
                    else:
                        st.caption("⏳ Analyserer ...")
            else:
                type_guess = ai_result.get("type_guess", "OTHER")
                sett       = ai_result.get("set_number")
                name       = ai_result.get("name")
                rb_img     = ai_result.get("_rb_img")
                rb_name    = ai_result.get("_rb_name")

                # ── SET / MINIFIG with identified number ─────────────────────
                if type_guess in ("SET", "MINIFIG") and sett:
                    st.caption("**AI-forslag:** sammenlign ditt bilde (venstre) med referansebildet (høyre)")
                    col_user, col_ref = st.columns(2)
                    with col_user:
                        user_bytes = st.session_state.get("reg_uploaded_img_bytes")
                        if user_bytes:
                            st.image(user_bytes, caption="Ditt bilde", use_container_width=True)
                    with col_ref:
                        if rb_img:
                            st.image(rb_img, caption=f"{rb_name or name} ({sett})",
                                     use_container_width=True)
                        else:
                            st.info(f"Fant ingen forhåndsvisning for {sett}.")
                    st.markdown(f"**Forslag:** {rb_name or name} — `{sett}`")
                    col_yes, col_no = st.columns(2)
                    with col_yes:
                        if st.button("✅ Stemmer — fortsett", type="primary",
                                     use_container_width=True):
                            st.session_state["reg_set_number"] = sett
                            with st.spinner("Henter info ..."):
                                _do_rb_fetch(sett)
                            st.rerun()
                    with col_no:
                        if st.button("❌ Stemmer ikke", use_container_width=True):
                            st.session_state["reg_ai_result"]        = None
                            st.session_state["reg_last_img_file_id"] = None
                            st.session_state["reg_input_mode"]       = "number"
                            st.rerun()

                # ── PART — show candidate gallery ────────────────────────────
                elif type_guess == "PART":
                    part_desc = ai_result.get("part_description", "")
                    st.info(f"🧩 AI gjenkjente en del: **{part_desc or 'ukjent type'}**")
                    candidates = ai_result.get("_part_candidates") or []
                    if candidates:
                        st.caption("Velg riktig del, eller fortsett manuelt:")
                        cols_per_row = 4
                        for row_start in range(0, len(candidates), cols_per_row):
                            row_parts = candidates[row_start:row_start + cols_per_row]
                            pcols = st.columns(cols_per_row)
                            for pcol, p in zip(pcols, row_parts):
                                with pcol:
                                    with st.container(border=True):
                                        if p.get("part_img_url"):
                                            st.image(p["part_img_url"], use_container_width=True)
                                        st.caption(f"**{p.get('name','')}**  \n`{p.get('part_num','')}`")
                                        if st.button("Velg", key=f"img_part_{p.get('part_num','')}",
                                                     use_container_width=True):
                                            st.session_state["reg_part_result"]   = p
                                            st.session_state["reg_input_mode"]    = "part"
                                            st.session_state["reg_ai_result"]     = None
                                            st.rerun()
                    else:
                        st.caption("Ingen treff på delnummer-søk.")
                    if st.button("🧩 Søk etter del manuelt", use_container_width=True):
                        st.session_state["reg_input_mode"] = "part"
                        st.session_state["reg_ai_result"]  = None
                        st.rerun()

                # ── Other identified types (BULK, INSTRUCTION, BOX, etc.) ────
                elif type_guess in ("BULK", "INSTRUCTION", "BOX", "GEAR", "CATALOG", "MOC"):
                    _type_labels = {
                        "BULK": "📦 Bulk / blanding",
                        "INSTRUCTION": "📋 Instruksjonsbok",
                        "BOX": "🗃️ Tom eske",
                        "GEAR": "👕 Merchandise / gear",
                        "CATALOG": "📒 Katalog",
                        "MOC": "🏗️ MOC / eget bygg",
                    }
                    st.info(f"AI gjenkjente: **{_type_labels.get(type_guess, type_guess)}**")
                    user_bytes = st.session_state.get("reg_uploaded_img_bytes")
                    if user_bytes:
                        st.image(user_bytes, width=200)
                    _mode_map = {"BULK": "bulk", "MOC": "moc"}
                    _target   = _mode_map.get(type_guess, "number")
                    col_ok, col_no = st.columns(2)
                    with col_ok:
                        if st.button(f"✅ Ja, registrer som {_type_labels.get(type_guess,'dette')}",
                                     type="primary", use_container_width=True):
                            st.session_state["reg_input_mode"] = _target
                            st.session_state["reg_ai_result"]  = None
                            st.rerun()
                    with col_no:
                        # Keep image — route to mode selector, not number path.
                        # Image bytes stay in session state so PART/BULK flows can reuse them.
                        if st.button("🔢 Velg type manuelt", use_container_width=True):
                            st.session_state["reg_ai_result"]  = None
                            st.session_state["reg_input_mode"] = None
                            st.rerun()

                # ── Fallback: nothing recognised ──────────────────────────────
                else:
                    st.warning("Kunne ikke identifisere objektet fra bildet.")
                    if ai_result.get("note"):
                        st.caption(ai_result["note"])
                    user_bytes = st.session_state.get("reg_uploaded_img_bytes")
                    if user_bytes:
                        st.image(user_bytes, width=200)
                    col_part, col_manual = st.columns(2)
                    with col_part:
                        if st.button("🧩 Søk etter del", type="primary",
                                     use_container_width=True):
                            st.session_state["reg_ai_result"]  = None
                            st.session_state["reg_input_mode"] = "part"
                            st.rerun()
                    with col_manual:
                        if st.button("🔢 Velg type manuelt", use_container_width=True):
                            st.session_state["reg_ai_result"]  = None
                            st.session_state["reg_input_mode"] = None
                            st.rerun()

            st.divider()
            _progress_indicator(step)

        # ── Number path ───────────────────────────────────────────────────────
        elif input_mode == "number":
            st.subheader("🔍 Settnummer eller navn")
            st.caption("Skriv inn settnummer (f.eks. 75192) eller navn (f.eks. 'Millennium Falcon')")

            def _on_set_num_change():
                st.session_state["rb_fetch_trigger"] = True

            set_num = st.text_input(
                "Settnummer eller navn",
                value=st.session_state["reg_set_number"] or "",
                placeholder="75192  —  eller  —  Millennium Falcon",
                key="_set_num_input",
                on_change=_on_set_num_change,
                label_visibility="collapsed",
            )

            if st.session_state.get("rb_fetch_trigger") and set_num.strip():
                st.session_state["rb_fetch_trigger"] = False
                if _looks_like_set_number(set_num):
                    st.session_state["reg_set_number"] = set_num.strip()
                with st.spinner("Søker ..."):
                    _do_rb_fetch(set_num.strip())
                st.rerun()

            if st.button("🔍 Søk", use_container_width=True,
                         type="primary", disabled=not set_num.strip()):
                st.session_state["rb_fetch_trigger"] = False
                if _looks_like_set_number(set_num):
                    st.session_state["reg_set_number"] = set_num.strip()
                with st.spinner("Søker ..."):
                    _do_rb_fetch(set_num.strip())
                st.rerun()

            # ── Variant gallery ───────────────────────────────────────────────
            if st.session_state["rb_status"] == "multiple":
                variants = st.session_state["rb_variants"] or []
                st.info(f"Fant {len(variants)} treff — velg riktig:")
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
                st.warning("Ikke funnet — gå videre og fyll inn manuelt.")
                if st.button("Gå videre →", type="primary"):
                    st.session_state["reg_step"] = 2
                    st.rerun()

            st.divider()
            _progress_indicator(step)

        # ── MOC path (eget bygg) ──────────────────────────────────────────────
        elif input_mode == "moc":
            st.subheader("🏗️ MOC – eget bygg")
            st.caption("Registrer noe du har bygget selv")
            st.divider()

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
                                st.session_state["moc_prefill"]    = m
                                st.session_state["moc_rb_results"] = []
                                st.rerun()
                elif st.session_state.get("moc_rb_results") is not None and moc_query:
                    st.caption("Ingen treff — fyll inn manuelt nedenfor.")

            prefill = st.session_state.get("moc_prefill") or {}
            name = st.text_input("Navn *", value=prefill.get("name", ""),
                                 placeholder="f.eks. Sopwith Camel 1:24")
            col1, col2 = st.columns(2)
            with col1:
                theme     = st.text_input("Tema", value=prefill.get("theme", "") or "")
                year      = st.number_input("År bygget", min_value=1949, max_value=2030,
                                            value=date.today().year, step=1)
                num_parts = st.number_input("Antall deler (estimat)", min_value=0, step=10,
                                            value=int(prefill.get("num_parts") or 0))
            with col2:
                condition = st.selectbox("Tilstand", list(CONDITION_LABEL.keys()),
                                         index=list(CONDITION_LABEL.keys()).index("BUILT"),
                                         format_func=lambda x: CONDITION_LABEL[x])
                notes = st.text_area("Notater", placeholder="Fri tekst ...")
            st.subheader("📐 Instruksjoner")
            instructions_url  = st.text_input("Lenke til instruksjoner",
                                               value=prefill.get("rebrickable_moc_url", "") or "",
                                               placeholder="https://rebrickable.com/mocs/MOC-12345/")
            instructions_file = st.file_uploader("Last opp instruksjonsfil (PDF, bilde)",
                                                  type=["pdf","jpg","jpeg","png"],
                                                  key="moc_instr_file")
            moc_id = prefill.get("moc_id", "") or ""

            if st.button("Neste →", use_container_width=True, type="primary"):
                if not name.strip():
                    st.error("Navn er påkrevd.")
                else:
                    st.session_state["pending_record"] = {
                        "object_type":        "MOC",
                        "set_number":         None,
                        "name":               name.strip(),
                        "theme":              theme.strip() or None,
                        "subtheme":           None,
                        "year":               int(year),
                        "condition":          condition,
                        "notes":              notes.strip() or None,
                        "num_parts":          int(num_parts) if num_parts else None,
                        "moc_base_set":       None,
                        "parent_object_id":   None,
                        "instructions_url":   instructions_url.strip() or None,
                        "rebrickable_moc_id": moc_id or None,
                        "_moc_instr_file":    instructions_file,
                    }
                    st.session_state["reg_step"] = 3
                    st.rerun()

        # ── MOD path (modifisert sett) ────────────────────────────────────────
        elif input_mode == "mod":
            st.subheader("🔧 MOD – modifisert sett")
            st.caption("Et offisielt sett du har bygget om eller lagt til noe på")
            st.divider()

            # Pre-filled parent from "Legg til MOD" button in detail_dialog
            _prefill_parent_id   = st.session_state.get("reg_mod_parent_id")
            _prefill_parent_name = st.session_state.get("reg_mod_parent_name")

            st.markdown("**Koble til originalsett (valgfritt)**")
            link_choice = st.radio(
                "Koblingstype",
                ["Fra samlingen min", "Ikke i samlingen (skriv inn nummer)"],
                index=0 if _prefill_parent_id else 0,
                horizontal=True,
                key="mod_link_type",
            )

            parent_object_id = None
            moc_base_set     = None

            if link_choice == "Fra samlingen min":
                if _prefill_parent_id and _prefill_parent_name:
                    st.success(f"Koblet til: **{_prefill_parent_name}**")
                    parent_object_id = _prefill_parent_id
                    if st.button("Endre kobling", key="mod_change_link"):
                        st.session_state["reg_mod_parent_id"]   = None
                        st.session_state["reg_mod_parent_name"] = None
                        st.rerun()
                else:
                    mod_search = st.text_input("Søk i samlingen",
                                               placeholder="75192 eller Millennium Falcon",
                                               key="mod_coll_search")
                    if mod_search.strip():
                        all_objs = fetch_objects()
                        q = mod_search.strip().lower()
                        matches = [o for o in all_objs
                                   if o.get("object_type") == "SET"
                                   and (q in (o.get("name") or "").lower()
                                        or q in (o.get("set_number") or ""))]
                        if matches:
                            chosen = st.selectbox(
                                "Velg originalsett",
                                matches,
                                format_func=lambda o: (
                                    f"{o.get('set_number','?')} – {display_name(o)}"
                                ),
                                key="mod_coll_pick",
                            )
                            if chosen:
                                parent_object_id = chosen.get("id")
                                st.caption(f"Valgt: {chosen.get('set_number','?')} – {display_name(chosen)}")
                        elif mod_search:
                            st.caption("Ingen treff i samlingen.")
            else:
                moc_base_set = st.text_input("Settnummer",
                                             placeholder="f.eks. 75192-1",
                                             key="mod_base_set_text")

            st.divider()
            name = st.text_input("Navn på MOD *", placeholder="f.eks. Millennium Falcon med LED")
            col1, col2 = st.columns(2)
            with col1:
                year      = st.number_input("År bygget", min_value=1949, max_value=2030,
                                            value=date.today().year, step=1)
                num_parts = st.number_input("Antall deler (estimat)", min_value=0, step=10)
            with col2:
                condition = st.selectbox("Tilstand", list(CONDITION_LABEL.keys()),
                                         index=list(CONDITION_LABEL.keys()).index("BUILT"),
                                         format_func=lambda x: CONDITION_LABEL[x])
                notes = st.text_area("Notater", placeholder="Hva er endret / lagt til?")

            instructions_file = st.file_uploader("Instruksjonsfil (valgfritt)",
                                                  type=["pdf","jpg","jpeg","png"],
                                                  key="mod_instr_file")

            if st.button("Neste →", use_container_width=True, type="primary"):
                if not name.strip():
                    st.error("Navn er påkrevd.")
                else:
                    st.session_state["pending_record"] = {
                        "object_type":        "MOD",
                        "set_number":         None,
                        "name":               name.strip(),
                        "theme":              None,
                        "subtheme":           None,
                        "year":               int(year),
                        "condition":          condition,
                        "notes":              notes.strip() or None,
                        "num_parts":          int(num_parts) if num_parts else None,
                        "moc_base_set":       moc_base_set.strip() if moc_base_set else None,
                        "parent_object_id":   parent_object_id,
                        "instructions_url":   None,
                        "rebrickable_moc_id": None,
                        "_moc_instr_file":    instructions_file,
                    }
                    st.session_state["reg_step"] = 3
                    st.rerun()

        # ── BULK path ─────────────────────────────────────────────────────────
        elif input_mode == "bulk":
            st.subheader("📦 Bulk – blanding")
            st.caption("En boks, pose eller haug med usortert LEGO")
            st.divider()

            bulk_name  = st.text_input(
                "Navn / beskrivelse *",
                placeholder="f.eks. Finn.no-kjøp mai 2026 eller Kjellerboks #3",
            )
            bulk_notes = st.text_area(
                "Innhold (fritekst)",
                placeholder="f.eks. Blanding City og Friends, ca 2 kg, inkl. noen minifigs",
            )
            bulk_weight = st.number_input("Vekt (kg, valgfritt)", min_value=0.0,
                                          step=0.1, format="%.1f")

            if st.button("Neste →", use_container_width=True, type="primary"):
                if not bulk_name.strip():
                    st.error("Navn er påkrevd.")
                else:
                    st.session_state["pending_record"] = {
                        "object_type":      "BULK",
                        "set_number":       None,
                        "name":             bulk_name.strip(),
                        "theme":            None,
                        "subtheme":         None,
                        "year":             date.today().year,
                        "condition":        "USED",
                        "notes":            bulk_notes.strip() or None,
                        "num_parts":        None,
                        "weight_kg":        float(bulk_weight) if bulk_weight else None,
                        "parent_object_id": None,
                    }
                    st.session_state["reg_step"] = 3
                    st.rerun()

        # ── PART path (løse deler) ────────────────────────────────────────────
        elif input_mode == "part":
            st.subheader("🧩 Løs del")
            st.caption("Registrer én del-type med farge og antall")
            st.divider()

            # Carry forward image from photo flow if available
            _part_cached_img   = st.session_state.get("reg_uploaded_img_bytes")
            _part_cached_type  = st.session_state.get("reg_uploaded_img_type")

            # Pre-selected part from photo flow
            part_result = st.session_state.get("reg_part_result")

            if part_result:
                c_img, c_info = st.columns([1, 3])
                with c_img:
                    if part_result.get("part_img_url"):
                        st.image(part_result["part_img_url"], use_container_width=True)
                with c_info:
                    st.success(f"**{part_result.get('name','')}**")
                    st.caption(f"Delnr: `{part_result.get('part_num','')}`")
                if st.button("Søk etter en annen del", key="part_reset_search"):
                    st.session_state["reg_part_result"] = None
                    st.rerun()
            else:
                # If there is a cached image and we haven't auto-searched yet,
                # run AI part identification automatically — no extra button click needed.
                if (_part_cached_img
                        and st.session_state.get("reg_part_search_results") is None
                        and not st.session_state.get("reg_part_ai_triggered")):
                    st.session_state["reg_part_ai_triggered"] = True
                    st.image(_part_cached_img, use_container_width=True)
                    with st.spinner("Identifiserer del fra bilde ..."):
                        ai_r = identify_lego_from_image(
                            _part_cached_img, _part_cached_type or "image/jpeg")
                        query = ai_r.get("part_search_query") or ai_r.get("part_description")
                        if query:
                            candidates = rb_search_parts(query, page_size=12)
                        else:
                            candidates = []
                    st.session_state["reg_part_search_results"] = candidates
                    st.rerun()
                elif _part_cached_img and st.session_state.get("reg_part_ai_triggered"):
                    # Show image as reference while user browses results / searches manually
                    st.image(_part_cached_img, width=120)

                part_query = st.text_input(
                    "Delnummer eller beskrivelse",
                    placeholder="3001  —  eller  —  2x4 brick",
                    key="part_search_input",
                )
                if st.button("🔍 Søk", key="part_search_btn",
                             type="primary", disabled=not part_query.strip()):
                    with st.spinner("Søker i Rebrickable ..."):
                        results = rb_search_parts(part_query.strip(), page_size=12)
                    st.session_state["reg_part_search_results"] = results
                    st.rerun()

                search_results = st.session_state.get("reg_part_search_results") or []
                if search_results:
                    st.caption(f"Fant {len(search_results)} treff — velg riktig del:")
                    cols_per_row = 4
                    for row_start in range(0, len(search_results), cols_per_row):
                        row_parts = search_results[row_start:row_start + cols_per_row]
                        pcols = st.columns(cols_per_row)
                        for pcol, p in zip(pcols, row_parts):
                            with pcol:
                                with st.container(border=True):
                                    if p.get("part_img_url"):
                                        st.image(p["part_img_url"], use_container_width=True)
                                    st.caption(f"**{p.get('name','')}**  \n`{p.get('part_num','')}`")
                                    if st.button("Velg", key=f"part_pick_{p.get('part_num','')}",
                                                 use_container_width=True):
                                        st.session_state["reg_part_result"]         = p
                                        st.session_state["reg_part_search_results"] = None
                                        st.rerun()
                elif st.session_state.get("reg_part_search_results") is not None:
                    st.caption("Ingen treff — prøv et annet søkeord.")

            # Color and quantity — shown when part is selected
            if part_result:
                st.divider()
                all_colors = rb_fetch_colors()
                if all_colors:
                    color_choice = st.selectbox(
                        "Farge",
                        all_colors,
                        format_func=lambda c: c.get("name", "?"),
                        key="part_color_pick",
                    )
                    _rgb = color_choice.get("rgb", "cccccc") if color_choice else "cccccc"
                    st.markdown(
                        f"<span style='display:inline-block;width:18px;height:18px;"
                        f"background:#{_rgb};border:1px solid #ccc;border-radius:3px;"
                        f"vertical-align:middle;margin-right:6px'></span>"
                        f"<span style='vertical-align:middle'>{color_choice.get('name','') if color_choice else ''}</span>",
                        unsafe_allow_html=True,
                    )
                else:
                    color_choice = None
                    st.text_input("Farge (fritekst)", key="part_color_text",
                                  placeholder="f.eks. Red")

                qty = st.number_input("Antall", min_value=1, value=1, step=1, key="part_qty")

                if st.button("Neste →", use_container_width=True, type="primary"):
                    color_id   = color_choice.get("id")   if color_choice else None
                    color_name = color_choice.get("name") if color_choice else (
                        st.session_state.get("part_color_text") or None)
                    st.session_state["pending_record"] = {
                        "object_type":      "PART",
                        "set_number":       part_result.get("part_num"),
                        "name":             part_result.get("name", ""),
                        "theme":            None,
                        "subtheme":         None,
                        "year":             date.today().year,
                        "condition":        "USED",
                        "notes":            None,
                        "num_parts":        int(qty),
                        "part_color_id":    color_id,
                        "part_color_name":  color_name,
                        "parent_object_id": None,
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
        # Show BL official name as a secondary caption when it differs
        # from the (collector-friendly) RB name. Saved as `name_bl`.
        _bl_secondary = st.session_state.get("bl_name_secondary")
        if _bl_secondary:
            st.caption(f"🏷️ BrickLink offisielt: *{_bl_secondary}*")
        theme       = st.text_input("Tema",   value=st.session_state["rb_theme"])
        subtheme    = st.text_input("Subtema", value=st.session_state["rb_subtheme"])
        year        = st.number_input("År", min_value=1949, max_value=2030,
                                      value=int(st.session_state["rb_year"]), step=1)
        # Tilstand as a prominent segmented control — no default, required.
        # Users must actively pick before saving, so a sealed box photo
        # never auto-defaults to BUILT or vice versa.
        st.markdown("**Tilstand \\***")
        st.caption("'Brukt' = bygget og lekt med, synlig slitasje")
        _cond_keys = list(CONDITION_LABEL.keys())
        try:
            condition = st.segmented_control(
                label="Tilstand",
                options=_cond_keys,
                format_func=lambda x: CONDITION_LABEL[x],
                selection_mode="single",
                default=st.session_state.get("reg_condition"),
                key="reg_condition_pick",
                label_visibility="collapsed",
            )
        except Exception:
            # Fallback for older Streamlit versions: use a row of st.buttons
            _cond_cols = st.columns(len(_cond_keys))
            for _c, _ck in zip(_cond_cols, _cond_keys):
                _is_sel = st.session_state.get("reg_condition") == _ck
                with _c:
                    if st.button(CONDITION_LABEL[_ck],
                                 use_container_width=True,
                                 type="primary" if _is_sel else "secondary",
                                 key=f"cond_btn_{_ck}"):
                        st.session_state["reg_condition"] = _ck
                        st.rerun()
            condition = st.session_state.get("reg_condition")
        else:
            # segmented_control returns the selection directly;
            # persist it so it survives reruns and can drive validation.
            st.session_state["reg_condition"] = condition

        # ── Slitasjegrad (collector grading) ───────────────────────────
        # Only meaningful for non-sealed conditions. Hide for SEALED.
        wear_level: str | None = None
        if condition in _WEAR_RELEVANT_CONDITIONS:
            st.markdown("**Slitasjegrad**")
            # Show AI suggestion note if pre-filled from photo flow
            _ai = st.session_state.get("reg_ai_result") or {}
            _ai_wear = _ai.get("wear_level")
            _ai_note = _ai.get("wear_note")
            if _ai_wear and _ai_wear in WEAR_LABEL:
                _msg = f"💡 AI-forslag fra bildet: **{WEAR_LABEL[_ai_wear]}**"
                if _ai_note:
                    _msg += f" — {_ai_note}"
                _msg += " (verifiser selv)"
                st.caption(_msg)
            else:
                st.caption("Hvor slitt er settet — fra som-ny til akseptabel")
            _wear_keys = list(WEAR_LABEL.keys())
            try:
                wear_level = st.segmented_control(
                    label="Slitasjegrad",
                    options=_wear_keys,
                    format_func=lambda x: WEAR_LABEL[x],
                    selection_mode="single",
                    default=st.session_state.get("reg_wear_level"),
                    key="reg_wear_pick",
                    label_visibility="collapsed",
                )
            except Exception:
                _wcols = st.columns(len(_wear_keys))
                for _wc, _wk in zip(_wcols, _wear_keys):
                    _is_w = st.session_state.get("reg_wear_level") == _wk
                    with _wc:
                        if st.button(WEAR_LABEL[_wk],
                                     use_container_width=True,
                                     type="primary" if _is_w else "secondary",
                                     key=f"wear_btn_{_wk}"):
                            st.session_state["reg_wear_level"] = _wk
                            st.rerun()
                wear_level = st.session_state.get("reg_wear_level")
            else:
                st.session_state["reg_wear_level"] = wear_level
        else:
            # Sealed → wear_level is not meaningful; clear any stale value
            st.session_state["reg_wear_level"] = None

        # ── Innhold & komplettering ──────────────────────────────────────
        st.markdown("**Innhold**")
        ic1, ic2 = st.columns(2)
        with ic1:
            reg_has_instructions = st.toggle(
                "Har instruksjoner",
                key="reg_has_instructions",
                help="Er den originale byggeinstruksjonen med?",
            )
        with ic2:
            reg_has_original_box = st.toggle(
                "Har original boks",
                key="reg_has_original_box",
                help="Er den originale esken med?",
            )
        _compl_keys  = list(COMPLETENESS_LABEL.keys())
        _cur_compl   = st.session_state.get("reg_completeness") or "UNKNOWN"
        if _cur_compl not in _compl_keys:
            _cur_compl = "UNKNOWN"
        reg_completeness = st.selectbox(
            "Mangler noe?",
            _compl_keys,
            index=_compl_keys.index(_cur_compl),
            format_func=lambda x: COMPLETENESS_LABEL[x],
            help="Mangler det deler, instruksjoner eller minifigurer?",
            key="reg_completeness_pick",
        )
        st.session_state["reg_completeness"] = reg_completeness

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
                elif not condition:
                    st.error("Tilstand er påkrevd — velg en verdi over.")
                else:
                    st.session_state["pending_record"] = {
                        "object_type": object_type,
                        "set_number":  st.session_state["reg_set_number"] or None,
                        "name":        name.strip(),
                        "theme":       theme.strip() or None,
                        "subtheme":    subtheme.strip() or None,
                        "year":        int(year),
                        "condition":   condition,
                        "wear_level":  st.session_state.get("reg_wear_level"),
                        "notes":       notes.strip() or None,
                        "num_parts":   st.session_state.get("rb_parts"),
                        "num_minifigs": st.session_state.get("rb_minifigs"),
                        "is_built":    condition in ("BUILT", "USED"),
                        "has_instructions":   reg_has_instructions,
                        "has_original_box":   reg_has_original_box,
                        "completeness_level": reg_completeness,
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

        # PART/BULK/MOC/MOD skip step 2 → Tilbake goes to step 1
        _back_to = 1 if st.session_state.get("reg_input_mode") in (
            "part", "bulk", "moc", "mod") else 2
        col_back, col_next = st.columns(2)
        with col_back:
            if st.button("← Tilbake", use_container_width=True):
                st.session_state["reg_step"] = _back_to
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

        # ── Auto-save cached image from AI flow as documentation ────────────
        cached_bytes = st.session_state.get("reg_uploaded_img_bytes")
        cached_type  = st.session_state.get("reg_uploaded_img_type")
        if (cached_bytes and obj_uuid
                and not st.session_state.get("reg_doc_img_saved")):
            with st.spinner("Lagrer bildet ..."):
                try:
                    ok = save_documentation_image(obj_uuid, ownership_id,
                                                  cached_bytes, cached_type)
                except Exception as e:
                    ok = False
                    st.error(f"Kunne ikke lagre bilde automatisk: {e}")
            if ok:
                st.session_state["reg_doc_img_saved"] = True

        if st.session_state.get("reg_doc_img_saved"):
            st.success("📷 Bildet ditt er lagret sammen med oppføringen.")
            if cached_bytes:
                st.image(cached_bytes, width=200, caption="Lagret bilde")

        # ── Manual upload (if no cached image, or user wants to replace) ────
        st.subheader("📷 Legg til eller bytt bilde (valgfri)")
        if st.session_state.get("reg_doc_img_saved"):
            st.caption("Bilde er allerede lagret. Last opp et nytt for å erstatte det.")
        else:
            st.caption("Et bilde gjør det lettere å kjenne igjen settet senere.")

        img_file = st.file_uploader(
            "Velg bilde",
            type=["jpg", "jpeg", "png", "webp"],
            key="reg_img_upload",
            label_visibility="collapsed",
        )
        if img_file and obj_uuid:
            if st.button("⬆️ Last opp bilde", use_container_width=True):
                with st.spinner("Laster opp ..."):
                    try:
                        ok = save_documentation_image(obj_uuid, ownership_id,
                                                      img_file.read(), img_file.type)
                    except Exception as e:
                        ok = False
                        st.error(f"Opplasting feilet: {e}")
                if ok:
                    st.session_state["reg_doc_img_saved"] = True
                    st.success("📷 Bilde lagret!")
                    st.rerun()
                elif ok is False and not img_file:
                    st.error("Opplasting feilet. Sjekk at storage-bucket er opprettet i Supabase.")

        if st.button("➕ Registrer et til", type="primary", use_container_width=True):
            reset_registration()
            st.rerun()
