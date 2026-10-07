r"""
Streamlit version of the drone airspace map (v2_full), behind one shared password.

The password comes from the MAP_PASSWORD environment variable (on Posit Connect: the app's
Settings > Vars). Map data is sent to the browser only after a correct password, and only for
the states picked in the sidebar.

Local run:   set MAP_PASSWORD=...   then   .venv\Scripts\streamlit run streamlit_app\app.py
Publish:     DEPLOY_TO_CONNECT.bat
"""

import hmac
import json
import os
import time
from pathlib import Path

import streamlit as st

HERE = Path(__file__).resolve().parent
SITE = HERE / "site" if (HERE / "site" / "index.html").exists() else HERE.parent / "v2_full"

st.set_page_config(page_title="Drone Airspace Map", page_icon="🛩️", layout="wide", initial_sidebar_state="expanded")
st.markdown("""<style>
  [data-testid="stToolbar"], footer { display:none; }
  /* map fills the window exactly, below Streamlit's 3.75rem header */
  .block-container, [data-testid="stMainBlockContainer"] { padding:3.75rem 0 0 0 !important; max-width:100% !important; }
  [data-testid="stMain"] iframe, [data-testid="stIFrame"] { display:block; border:0; height:calc(100vh - 3.75rem) !important; }
  [data-testid="stMain"] [data-testid="stVerticalBlock"] { gap:0 !important; }
  [data-testid="stMain"] [data-testid="stElementContainer"]:has(iframe) { height:auto !important; }
  [data-testid="stMain"] { overflow:hidden; }
</style>""", unsafe_allow_html=True)


def require_password():
    if st.session_state.get("authed"):
        return
    expected = os.environ.get("MAP_PASSWORD", "")
    _, mid, _ = st.columns([1, 1, 1])
    with mid:
        st.markdown("<div style='height:18vh'></div>", unsafe_allow_html=True)
        st.markdown("### 🛩️ Drone Airspace Map")
        if not expected:
            st.error("The MAP_PASSWORD setting is missing on the server, so the map is locked.")
            st.stop()
        with st.form("login"):
            pw = st.text_input("Password", type="password")
            ok = st.form_submit_button("Open map", use_container_width=True)
        if ok:
            if hmac.compare_digest(pw.encode(), expected.encode()):
                st.session_state.authed = True
                st.rerun()
            time.sleep(1.5)                       # slow down guessing
            st.error("Wrong password.")
    st.stop()


@st.cache_data(show_spinner=False)
def load_index(mtime):
    """data/states.js = 'window.UAS_STATES=[...];window.UAS_META={...};'"""
    txt = (SITE / "data" / "states.js").read_text(encoding="utf-8")
    a, b = txt.split(";window.UAS_META=", 1)
    return json.loads(a.split("=", 1)[1]), json.loads(b.rstrip().rstrip(";"))


@st.cache_data(show_spinner=False, max_entries=80)
def state_js(code, mtime):
    return (SITE / "data" / "states" / f"{code}.js").read_text(encoding="utf-8")


require_password()

if not (SITE / "data" / "states.js").exists():
    st.error(f"Map data not found in {SITE}. Build it with make_v2_full.py first.")
    st.stop()

mtime = (SITE / "data" / "states.js").stat().st_mtime
states, meta = load_index(mtime)
by_name = {s["name"]: s for s in states}
names = [s["name"] for s in states]

with st.sidebar:
    st.markdown("### 🛩️ Drone Airspace Map")
    default = st.session_state.get("states") or [n for n in names if by_name[n]["code"] in (meta.get("defaultStates") or ["NY"])]
    picked = st.multiselect("States to show", names, default=default,
                            help="Only these states are loaded into the map. Fewer states = faster.")
    st.session_state.states = picked
    size = sum(by_name[n]["mb"] for n in picked)
    st.caption(f"{len(picked)} state(s), about {size:.0f} MB")
    if size > 40:
        st.warning("That's a lot of data; the map may be slow. Pick fewer states for speed.")
    if st.button("Log out", use_container_width=True):
        st.session_state.clear()
        st.rerun()

if not picked:
    st.info("Pick at least one state in the sidebar.")
    st.stop()

codes = [by_name[n]["code"] for n in picked]
sel = [by_name[n] for n in picked]
view = [min(s["bbox"][0] for s in sel), min(s["bbox"][1] for s in sel), max(s["bbox"][2] for s in sel), max(s["bbox"][3] for s in sel)]
js = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
head = ("<script>"
        f"window.UAS_STATES={js(sel)};"
        f"window.UAS_META={js({**meta, 'bbox': view, 'defaultStates': codes})};"
        f"try{{localStorage.setItem('uas_states',{js(json.dumps(codes))})}}catch(e){{}}"
        "</script>\n"
        + "".join(f"<script>{state_js(c, mtime)}</script>\n" for c in codes))
html = (SITE / "index.html").read_text(encoding="utf-8").replace('<script src="data/states.js"></script>', head, 1)
with st.spinner("Loading map..."):
    st.iframe(html, height=800)          # CSS above stretches it to the window
