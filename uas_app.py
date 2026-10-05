r"""
uas_app.py - AirHub-style drone airspace map (Streamlit).

Run:  .venv\Scripts\streamlit run uas_app.py
      (opens http://localhost:8501 in your browser)

Reads out/uas_rules.gpkg made by build_uas_layers.py. The sidebar (arrow at top-left)
can download/refresh the data for an area. Click anywhere on the map for a flight briefing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import requests
import streamlit as st

import make_uas_map as mm

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
GPKG = OUT / "uas_rules.gpkg"
FAA = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
PRESETS = {
    "New York State": (-79.8, 40.4, -71.8, 45.1),
    "New York City": (-74.3, 40.45, -73.65, 40.95),
    "Washington DC area": (-77.6, 38.5, -76.9, 39.1),
    "Custom": None,
}

st.set_page_config(page_title="Drone Airspace", page_icon="🛩️", layout="wide", initial_sidebar_state="collapsed")
st.markdown("""<style>
  [data-testid="stHeader"] { background: transparent; }
  [data-testid="stToolbar"], footer { display: none; }
  .block-container, [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: 100% !important; }
  [data-testid="stMain"] iframe { display: block; border: 0; }
  [data-testid="stSidebar"] { background: #1d1d20; border-right: 1px solid #34353a; }
</style>""", unsafe_allow_html=True)


# ----------------------------------------------------------------------------- data

@st.cache_data(show_spinner="Loading airspace layers…")
def load_rules(path, mtime):
    return mm.load_layers(Path(path)), mm.load_manifest(Path(path).with_name("manifest.csv"))


@st.cache_data(ttl=7 * 24 * 3600, show_spinner="Loading airports…")
def load_airports(bbox):
    url, out, offset = f"{FAA}/US_Airport/FeatureServer/0/query", [], 0
    while True:
        r = requests.get(url, timeout=60, params={
            "where": "OPERSTATUS IS NULL OR OPERSTATUS <> 'CLOSED'",
            "outFields": "IDENT,ICAO_ID,NAME,TYPE_CODE,PRIVATEUSE,MIL_CODE,SERVCITY,STATE,ELEVATION",
            "geometry": ",".join(map(str, bbox)), "geometryType": "esriGeometryEnvelope", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects", "outSR": 4326, "f": "json",
            "resultOffset": offset, "resultRecordCount": 1000}).json()
        feats = r.get("features") or []
        for f in feats:
            a, g = f["attributes"], f.get("geometry") or {}
            if "x" in g:
                out.append({"x": round(g["x"], 5), "y": round(g["y"], 5), "id": a.get("IDENT"), "icao": a.get("ICAO_ID"),
                            "name": a.get("NAME"), "type": a.get("TYPE_CODE"), "private": a.get("PRIVATEUSE") == 1,
                            "mil": a.get("MIL_CODE"), "city": a.get("SERVCITY"), "state": a.get("STATE"),
                            "elev": a.get("ELEVATION")})
        if not feats or not r.get("exceededTransferLimit"):
            break
        offset += len(feats)
    return out


def run_build(bbox, group=None):
    cmd = [sys.executable, "-u", str(HERE / "build_uas_layers.py"), f"--bbox={','.join(map(str, bbox))}"]
    if group:
        cmd.append(f"--group={group}")
    with st.status("Downloading FAA data…", expanded=True) as status:
        box, lines = st.empty(), []
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"})
        for line in proc.stdout:
            line = line.rstrip()
            if line and not line.endswith("..."):
                lines.append(line)
                box.code("\n".join(lines[-14:]), language=None)
        ok = proc.wait() == 0
        status.update(label="Data updated" if ok else "Finished with errors (see log)", state="complete" if ok else "error")
    return ok


# ----------------------------------------------------------------------------- sidebar

manifest_bbox = None
if (OUT / "manifest.csv").exists():
    rows = mm.load_manifest(OUT / "manifest.csv")
    manifest_bbox = next((r["bbox"] for r in rows if r.get("bbox") and r["bbox"] != "full"), None)

with st.sidebar:
    st.markdown("### 🛩️ Airspace data")
    area = st.selectbox("Area", list(PRESETS), index=0)
    if PRESETS[area]:
        bbox = PRESETS[area]
    else:
        txt = st.text_input("Bounding box (minLon,minLat,maxLon,maxLat)", manifest_bbox or "-79.8,40.4,-71.8,45.1")
        try:
            bbox = tuple(float(v) for v in txt.split(","))
            assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
        except (ValueError, AssertionError):
            st.error("Use four numbers: minLon,minLat,maxLon,maxLat")
            st.stop()
    c1, c2 = st.columns(2)
    if c1.button("Download all", width="stretch", help="All layers for this area (a few minutes)"):
        run_build(bbox)
        st.rerun()
    if c2.button("Refresh TFRs", width="stretch", help="Fast group: TFRs, stadium events, NOTAMs"):
        run_build(bbox, "fast")
        st.rerun()
    if manifest_bbox:
        st.caption(f"Loaded data covers: `{manifest_bbox}`")
    height = st.slider("Map height (px)", 600, 1400, 880, 20)

if not GPKG.exists():
    st.markdown("## No data yet")
    st.write("Open the sidebar (arrow, top-left), pick an area and click **Download all**.")
    st.stop()

data, manifest = load_rules(str(GPKG), GPKG.stat().st_mtime)
data_bbox = tuple(float(v) for v in manifest_bbox.split(",")) if manifest_bbox else bbox
try:
    airports = load_airports(data_bbox)
except Exception as e:
    airports = []
    st.sidebar.warning(f"Airports unavailable: {e}")

with st.sidebar:
    st.markdown("#### Data status")
    st.dataframe([{"layer": r["layer"], "status": r["status"], "count": r["count"],
                   "FAA last edit": (r.get("server_last_edit") or "")[:16].replace("T", " ")} for r in manifest],
                 hide_index=True, width="stretch")

meta = {"bbox": list(data_bbox), "manifest": manifest}
js = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
html = (Path(HERE / "uas_app_map.html").read_text(encoding="utf-8")
        .replace("__DATA__", js(data)).replace("__AIRPORTS__", js(airports)).replace("__META__", js(meta)))
st.iframe(html, height=height)
