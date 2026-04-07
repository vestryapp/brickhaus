"""
BrickHaus – Streamlit app
Phase 1: View, search and register Lego objects.
Mobile-first registration with Rebrickable auto-fill.

Run:  streamlit run app/main.py
"""

import os
import io
import json
import base64
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
import requests
from requests_oauthlib import OAuth1
import streamlit as st
from PIL import Image, ImageDraw

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL    = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
REBRICKABLE_KEY     = os.environ.get("REBRICKABLE_API_KEY", "")
BL_CONSUMER_KEY     = os.environ.get("BRICKLINK_CONSUMER_KEY", "")
BL_CONSUMER_SECRET  = os.environ.get("BRICKLINK_CONSUMER_SECRET", "")
BL_TOKEN            = os.environ.get("BRICKLINK_TOKEN", "")
BL_TOKEN_SECRET     = os.environ.get("BRICKLINK_TOKEN_SECRET", "")

def _bl_auth():
    return OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)

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

def bl_get_price(set_number: str, condition: str) -> float | None:
    """Fetch average BrickLink price (NOK) for a set."""
    if not BL_CONSUMER_KEY:
        return None
    # BrickLink expects full number incl. variant: "75192" → "75192-1"
    num = set_number if "-" in set_number else f"{set_number}-1"
    new_or_used = "N" if condition == "SEALED" else "U"
    try:
        r = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/SET/{num}/price",
            auth=_bl_auth(),
            params={
                "guide_type":   "sold",
                "new_or_used":  new_or_used,
                "currency_code": "NOK",
                "region":        "europe",
            },
            timeout=10,
        )
        if not r.ok:
            return None
        pg = r.json().get("data", {}).get("price_detail", [])
        avg = r.json().get("data", {}).get("avg_price")
        return float(avg) if avg else None
    except Exception:
        return None

def rb_lookup(set_number: str) -> dict | None:
    num = set_number.strip()
    if "-" not in num:
        num = f"{num}-1"
    try:
        r = requests.get(
            f"https://rebrickable.com/api/v3/lego/sets/{num}/",
            headers=RB_HEADERS, timeout=8,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()

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
    except Exception:
        return None


# ── Supabase helpers ──────────────────────────────────────────────────────────

@st.cache_data(ttl=60)
def fetch_objects():
    rows, offset, limit = [], 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/objects",
            headers={**SB_HEADERS, "Range-Unit": "items", "Range": f"{offset}-{offset+limit-1}"},
            params={"select": "ownership_id,object_type,set_number,name,theme,subtheme,year,condition,status,location_id,sub_location,estimated_value_bl,total_cost_nok,quality_level,notes,insured,purchase_price,purchase_currency,purchase_date,purchase_source,registered_at"},
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


# ── Display helpers ───────────────────────────────────────────────────────────

CONDITION_LABEL = {
    "SEALED":     "🔒 Forseglet",
    "OPENED":     "📦 Åpnet ubygget",
    "BUILT":      "🔨 Bygget",
    "USED":       "🪖 Brukt",
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


# ── Session state ─────────────────────────────────────────────────────────────

def init_state():
    defaults = {
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
        "rb_fetch_trigger": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

init_state()

def reset_registration():
    keys = ["rb_name","rb_theme","rb_subtheme","rb_img","rb_parts","rb_status",
            "reg_set_number","pending_record","confirm_no_loc","rb_fetch_trigger"]
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

    col1, col2 = st.columns(2)
    with col1:
        is_moc = obj.get("object_type") in ("MOC", "MOD")
        name = st.text_input("Navn *", value=obj.get("name") or "",
                             disabled=not is_moc,
                             help="Navn kan kun redigeres for MOC og Mod" if not is_moc else None)
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
            updates = {
                "name":              name.strip() if is_moc else obj.get("name"),
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
                "purchase_source":   purchase_source.strip() or None,
                "total_cost_nok":    total_nok,
            }
            sb_patch("objects", {"ownership_id": f"eq.{oid}"}, updates)
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

    # BrickLink price sync
    if BL_CONSUMER_KEY:
        missing_price = [o for o in objects
                         if o.get("object_type") == "SET"
                         and o.get("set_number")
                         and not o.get("estimated_value_bl")]
        if missing_price:
            st.caption(f"💰 {len(missing_price)} sett mangler BrickLink-pris")
            if st.button("🔄 Hent BrickLink-priser", type="secondary"):
                progress = st.progress(0, text="Henter priser ...")
                updated = 0
                for i, obj in enumerate(missing_price):
                    price = bl_get_price(obj["set_number"], obj.get("condition", "USED"))
                    if price:
                        sb_patch("objects",
                                 {"ownership_id": f"eq.{obj['ownership_id']}"},
                                 {"estimated_value_bl": price})
                        updated += 1
                    progress.progress((i + 1) / len(missing_price),
                                      text=f"Hentet {i+1}/{len(missing_price)} ...")
                progress.empty()
                st.cache_data.clear()
                st.success(f"✅ Oppdaterte pris for {updated} av {len(missing_price)} sett")
                st.rerun()

    st.divider()

    if not filtered:
        st.info("Ingen objekter matcher filteret.")
    else:
        st.caption("Klikk en rad for å se detaljer og redigere.")
        rows = [{
            "ID":       o.get("ownership_id", ""),
            "Type":     TYPE_LABEL.get(o.get("object_type", ""), ""),
            "Settnr.":  o.get("set_number") or "–",
            "Navn":     o.get("name") or "–",
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
            column_config={"Notater": st.column_config.TextColumn(width="medium")},
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

    # ── STEP 1: Set number ────────────────────────────────────────────────────
    if step == 1:
        st.subheader("Settnummer")

        # on_change fires when Enter is pressed or field loses focus
        def _on_set_num_change():
            st.session_state["rb_fetch_trigger"] = True

        set_num = st.text_input(
            "Settnummer",
            value=st.session_state["reg_set_number"],
            placeholder="f.eks. 75192",
            key="_set_num_input",
            on_change=_on_set_num_change,
        )

        # Resolve Enter-key trigger (fires before buttons render)
        if st.session_state.get("rb_fetch_trigger") and set_num.strip():
            st.session_state["rb_fetch_trigger"] = False
            st.session_state["reg_set_number"] = set_num.strip()
            with st.spinner("Henter ..."):
                data = rb_lookup(set_num.strip())
            if data:
                st.session_state["rb_name"]     = data.get("name", "")
                st.session_state["rb_theme"]    = data.get("_theme_name", "")
                st.session_state["rb_subtheme"] = data.get("_subtheme_name", "")
                st.session_state["rb_year"]     = data.get("year", date.today().year)
                st.session_state["rb_img"]      = data.get("set_img_url")
                st.session_state["rb_parts"]    = data.get("num_parts")
                st.session_state["rb_status"]   = "found"
                st.session_state["reg_step"]    = 2
                st.rerun()
            else:
                st.session_state["rb_status"] = "not_found"
                st.warning("Ikke funnet i Rebrickable — gå videre og fyll inn manuelt.")

        col_a, col_b = st.columns([3, 1])
        with col_a:
            fetch_btn = st.button("🔍 Hent info fra Rebrickable", use_container_width=True,
                                  type="primary", disabled=not set_num.strip())
        with col_b:
            skip_btn = st.button("MOC / løse deler", use_container_width=True)

        if fetch_btn and set_num.strip():
            st.session_state["rb_fetch_trigger"] = False
            st.session_state["reg_set_number"] = set_num.strip()
            with st.spinner("Henter ..."):
                data = rb_lookup(set_num.strip())
            if data:
                st.session_state["rb_name"]     = data.get("name", "")
                st.session_state["rb_theme"]    = data.get("_theme_name", "")
                st.session_state["rb_subtheme"] = data.get("_subtheme_name", "")
                st.session_state["rb_year"]     = data.get("year", date.today().year)
                st.session_state["rb_img"]      = data.get("set_img_url")
                st.session_state["rb_parts"]    = data.get("num_parts")
                st.session_state["rb_status"]   = "found"
                st.session_state["reg_step"]    = 2
                st.rerun()
            else:
                st.session_state["rb_status"] = "not_found"
                st.warning("Ikke funnet i Rebrickable — gå videre og fyll inn manuelt.")

        if skip_btn:
            st.session_state["reg_set_number"] = set_num.strip()
            st.session_state["reg_step"] = 2
            st.rerun()

        if st.session_state["rb_status"] == "not_found":
            if st.button("Gå videre →", type="primary"):
                st.session_state["reg_step"] = 2
                st.rerun()

        st.divider()
        _progress_indicator(step)

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

    # ── STEP 5: Save ─────────────────────────────────────────────────────────
    elif step == 5:
        rec = st.session_state["pending_record"].copy()

        with st.spinner("Lagrer ..."):
            loc_name = rec.pop("_loc_name", None)
            loc_id   = get_or_create_location(loc_name) if loc_name else None

            # Fetch BrickLink price silently if we have a set number
            bl_price = None
            if rec.get("set_number") and rec.get("object_type") == "SET":
                bl_price = bl_get_price(rec["set_number"], rec.get("condition", "USED"))

            ownership_id = next_ownership_id()
            record = {
                **rec,
                "ownership_id":       ownership_id,
                "status":             "OWNED",
                "location_id":        loc_id,
                "registered_at":      str(date.today()),
                "quality_level":      "BASIC",
                "estimated_value_bl": bl_price,
            }
            save_object(record)

        st.success(f"✅ **{rec['name']}** lagret som **{ownership_id}**")

        if st.button("➕ Registrer et til", type="primary", use_container_width=True):
            reset_registration()
            st.rerun()
