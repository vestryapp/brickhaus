"""
BrickHaus – Streamlit app
Phase 1: View, search and register Lego objects.
Mobile-first registration with Rebrickable auto-fill.

Run:  streamlit run app/main.py
"""

import os
import json
from datetime import date
from pathlib import Path
from dotenv import load_dotenv
import requests
import streamlit as st

load_dotenv(Path(__file__).parent.parent / ".env")

SUPABASE_URL    = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY    = os.environ["SUPABASE_SERVICE_KEY"]
REBRICKABLE_KEY = os.environ.get("REBRICKABLE_API_KEY", "")

SB_HEADERS = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}
RB_HEADERS = {"Authorization": f"key {REBRICKABLE_KEY}"}

st.set_page_config(page_title="BrickHaus", page_icon="🧱", layout="wide")


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
            params={"select": "ownership_id,object_type,set_number,name,theme,subtheme,year,condition,status,location_id,sub_location,estimated_value_bl,total_cost_nok,quality_level,notes,insured"},
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


# ══════════════════════════════════════════════════════════════════════════════
# APP
# ══════════════════════════════════════════════════════════════════════════════

st.title("🧱 BrickHaus")

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

    n_sets    = sum(1 for o in filtered if o.get("object_type") == "SET")
    n_figs    = sum(1 for o in filtered if o.get("object_type") == "MINIFIG")
    n_moc     = sum(1 for o in filtered if o.get("object_type") in ("MOC", "MOD"))
    total_cost = sum(o.get("total_cost_nok") or 0 for o in filtered)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sett", f"{n_sets}")
    c2.metric("Minifigurer", f"{n_figs}")
    c3.metric("MOC / Mod", f"{n_moc}")
    c4.metric("Total kostpris", fmt_nok(total_cost))
    st.divider()

    if not filtered:
        st.info("Ingen objekter matcher filteret.")
    else:
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
        st.dataframe(rows, use_container_width=True, height=600,
                     column_config={"Notater": st.column_config.TextColumn(width="medium")})


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

            ownership_id = next_ownership_id()
            record = {
                **rec,
                "ownership_id":  ownership_id,
                "status":        "OWNED",
                "location_id":   loc_id,
                "registered_at": str(date.today()),
                "quality_level": "BASIC",
            }
            save_object(record)

        st.success(f"✅ **{rec['name']}** lagret som **{ownership_id}**")

        if st.button("➕ Registrer et til", type="primary", use_container_width=True):
            reset_registration()
            st.rerun()
