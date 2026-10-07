r"""
make_uas_map.py
Turn out/uas_rules.gpkg (from build_uas_layers.py) into one interactive HTML map.

Click anywhere: the side panel lists EVERY rule at that spot (all layers, even hidden ones),
worst first, with a plain-English explanation, altitudes, active times, citation, notes and
all original FAA fields.

Run:   .venv\Scripts\python make_uas_map.py --title "New York"
Open:  out\uas_map.html (double-click; needs internet for the basemap tiles)

Best for a state or metro area. A full-US GeoPackage makes a very large HTML file.
"""

import argparse
import csv
import json
import math
import re
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

HERE = Path(__file__).resolve().parent

# drawing order, bottom -> top; tolerance in degrees for simplification (~1e-4 = 10 m)
LAYER_ORDER = ["fria", "recreational_fixed_sites", "class_airspace", "laanc_grid", "special_use_airspace",
               "stadiums_3nm", "seams", "nda_tfr", "nsufr_part_time", "nsufr", "prohibited_areas",
               "notam_tfr", "tfr_active"]
SIMPLIFY = {"class_airspace": 2e-4, "special_use_airspace": 1e-4, "nsufr": 3e-5, "laanc_grid": 0}
DATE_KEY = re.compile(r"date|time|edit|eff|updated", re.I)


def clean(v):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    if hasattr(v, "item"):
        v = v.item()
    return v


def rnd(c, nd=5):
    return [round(c[0], nd), round(c[1], nd)] if isinstance(c[0], (int, float)) else [rnd(x, nd) for x in c]


def fields_from(raw):
    try:
        d = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    out = {}
    for k, v in d.items():
        if v in (None, "", " ") or k.startswith("Shape__"):
            continue
        if isinstance(v, (int, float)) and DATE_KEY.search(k) and 9e11 < v < 4.2e12:
            v = pd.Timestamp(v, unit="ms", tz="UTC").strftime("%Y-%m-%d %H:%M UTC")
        out[k] = v
    return out


def dissolve_laanc(g):
    """21k grid squares -> one shape per (airport, LAANC ceiling)."""
    a = g["source_attrs"].map(lambda s: json.loads(s) if s else {})
    g = g.assign(_apt=a.map(lambda x: x.get("APT1_FAAID") or "?"), _n=1,
                 _f=a.map(lambda x: {k: x.get(k) for k in ("APT1_NAME", "APT1_FAAID", "APT1_LAANC", "CEILING",
                                                           "UNIT", "AIRSPACE_1", "REGION", "MAP_EFF", "LAST_EDIT")}))
    agg = {c: "first" for c in g.columns if c not in ("geometry", "_apt", "ceiling_ft", "_n")}
    agg["_n"] = "sum"
    d = g.dissolve(by=["_apt", "ceiling_ft"], aggfunc=agg, dropna=False).reset_index()
    d["source_attrs"] = [json.dumps({**f, "grid_cells": int(n)}) for f, n in zip(d["_f"], d["_n"])]
    return d.drop(columns=["_apt", "_n", "_f"])


def load_layers(gpkg, simplify=None, precision=5):
    """simplify: per-layer tolerance overrides (degrees); precision: coordinate decimals."""
    tols = {**SIMPLIFY, **(simplify or {})}
    have = {name for name, _ in pyogrio.list_layers(gpkg)}
    data = {}
    for name in LAYER_ORDER:
        if name not in have:
            continue
        g = gpd.read_file(gpkg, layer=name)
        if g.empty:
            data[name] = []
            continue
        if name == "laanc_grid":
            g = dissolve_laanc(g)
        tol = tols.get(name, tols.get("*", 2e-5))
        if tol:
            g["geometry"] = g.geometry.simplify(tol, preserve_topology=True)
        g = g[~g.geometry.is_empty & g.geometry.notna()]
        feats = []
        for row in g.itertuples(index=False):
            p = {c: clean(getattr(row, c)) for c in g.columns if c not in ("geometry", "source_attrs")}
            p["fields"] = fields_from(getattr(row, "source_attrs", None))
            geom = row.geometry.__geo_interface__
            feats.append({"type": "Feature", "properties": p,
                          "geometry": {"type": geom["type"], "coordinates": rnd(geom["coordinates"], precision)}})
        data[name] = feats
        print(f"  {name:<26}{len(feats):>6} shapes")
    return data


def load_manifest(path):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [{k: r.get(k) for k in ("layer", "status", "count", "server_last_edit", "fetched_utc", "bbox", "message")}
                for r in csv.DictReader(f)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[1])
    ap.add_argument("--gpkg", default=str(HERE / "out" / "uas_rules.gpkg"))
    ap.add_argument("--out", default=None, help="output HTML (default: uas_map.html next to the GeoPackage)")
    ap.add_argument("--title", default="", help='area name shown in the header, e.g. "New York"')
    args = ap.parse_args()

    gpkg = Path(args.gpkg)
    out = Path(args.out) if args.out else gpkg.with_name("uas_map.html")
    print(f"Reading {gpkg}")
    data = load_layers(gpkg)
    manifest = load_manifest(gpkg.with_name("manifest.csv"))
    bbox = next((r["bbox"] for r in manifest if r.get("bbox") and r["bbox"] != "full"), None)
    meta = {"title": args.title, "manifest": manifest,
            "bbox": [float(x) for x in bbox.split(",")] if bbox else None,
            "generated": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")}

    js = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = (TEMPLATE.replace("__TITLE__", (args.title + " – " if args.title else "") + "Drone Airspace Rules")
            .replace("__DATA__", js(data)).replace("__META__", js(meta)))
    out.write_text(html, encoding="utf-8")
    mb = out.stat().st_size / 1e6
    print(f"\nWrote {out}  ({mb:.1f} MB)" + ("  -- large; consider a smaller --bbox" if mb > 60 else ""))
    print("Double-click it to open in your browser.")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { --bg:#f6f7f9; --panel:#fff; --ink:#1d2330; --muted:#667085; --line:#e4e7ec; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; color:var(--ink); background:var(--bg); }
  #app { display:grid; grid-template-columns:300px 1fr 400px; grid-template-rows:56px 1fr; height:100vh; }
  header { grid-column:1/4; display:flex; align-items:center; gap:16px; padding:0 18px; background:#111827; color:#fff; }
  header h1 { font-size:17px; margin:0; font-weight:650; letter-spacing:.2px; }
  header .sub { color:#9ca3af; font-size:12.5px; }
  header .sp { flex:1; }
  header button { background:#1f2937; color:#e5e7eb; border:1px solid #374151; border-radius:6px; padding:6px 10px; cursor:pointer; font:inherit; font-size:12.5px; }
  header button.on { background:#e5e7eb; color:#111827; }
  #layers, #info { background:var(--panel); overflow-y:auto; }
  #layers { border-right:1px solid var(--line); padding:12px 14px 20px; }
  #info { border-left:1px solid var(--line); padding:14px 16px 24px; }
  #map { height:100%; }
  .grp { font-size:11.5px; text-transform:uppercase; letter-spacing:.6px; color:var(--muted); margin:16px 0 6px; font-weight:650; }
  .lyr { display:grid; grid-template-columns:18px 16px 1fr auto; gap:8px; align-items:start; padding:7px 4px; border-radius:6px; cursor:pointer; }
  .lyr:hover { background:#f2f4f7; }
  .lyr input { margin:2px 0 0; }
  .sw { width:16px; height:16px; border-radius:4px; margin-top:1px; border:2px solid; }
  .lyr .t { font-weight:600; font-size:13.5px; }
  .lyr .d { color:var(--muted); font-size:12px; margin-top:1px; }
  .lyr .n { color:var(--muted); font-size:12px; font-variant-numeric:tabular-nums; }
  .lyr.empty { opacity:.45; }
  .ramp { display:flex; gap:2px; margin-top:5px; font-size:10.5px; color:var(--muted); }
  .ramp span { flex:1; text-align:center; padding:2px 0; border-radius:3px; color:#1d2330; }
  .fresh { margin-top:18px; font-size:11.5px; color:var(--muted); border-top:1px solid var(--line); padding-top:10px; }
  .hint { color:var(--muted); text-align:center; margin-top:40px; }
  .hint b { display:block; font-size:16px; color:var(--ink); margin-bottom:6px; }
  .verdict { border-radius:10px; padding:12px 14px; color:#fff; margin-bottom:12px; }
  .verdict .big { font-size:17px; font-weight:700; }
  .verdict .small { font-size:12.5px; opacity:.92; margin-top:3px; }
  .coord { font-size:12px; color:var(--muted); margin:-4px 0 12px; }
  .card { border:1px solid var(--line); border-left:5px solid var(--c); border-radius:8px; padding:10px 12px; margin-bottom:10px; background:#fff; }
  .card-h { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .badge { background:var(--c); color:#fff; font-size:11px; font-weight:700; padding:2px 7px; border-radius:20px; text-transform:uppercase; letter-spacing:.4px; }
  .lt { font-size:12px; color:var(--muted); font-weight:600; }
  .nm { font-weight:650; font-size:14.5px; margin:5px 0 4px; }
  .what { font-size:13px; color:#344054; margin-bottom:8px; }
  dl { display:grid; grid-template-columns:86px 1fr; gap:3px 10px; margin:0; font-size:12.5px; }
  dt { color:var(--muted); }
  dd { margin:0; word-break:break-word; }
  .st { display:inline-block; font-size:11px; font-weight:700; padding:1px 6px; border-radius:4px; margin-left:4px; }
  .st.active { background:#fee2e2; color:#b91c1c; } .st.upcoming { background:#fef3c7; color:#92400e; }
  .st.ended { background:#e5e7eb; color:#4b5563; } .st.perm { background:#e0e7ff; color:#3730a3; }
  details { margin-top:7px; font-size:12.5px; }
  summary { cursor:pointer; color:#2563eb; font-weight:600; }
  details p { white-space:pre-wrap; margin:6px 0 0; color:#344054; }
  table.f { border-collapse:collapse; width:100%; margin-top:6px; font-size:12px; }
  table.f td { border-top:1px solid var(--line); padding:3px 4px; vertical-align:top; word-break:break-word; }
  table.f td:first-child { color:var(--muted); width:40%; }
  .ev { display:flex; justify-content:space-between; gap:8px; border-top:1px solid var(--line); padding:5px 0; font-size:12.5px; }
  .ev:first-child { border-top:0; }
  .leaflet-container { background:#e8ecef; }
  @media (max-width:1100px) { #app { grid-template-columns:1fr; grid-template-rows:56px 55vh auto auto; height:auto; }
    header { grid-column:1; } #map { height:55vh; } #layers,#info { border:0; } }
</style>
</head>
<body>
<div id="app">
  <header>
    <h1>__TITLE__</h1><span class="sub" id="sub"></span><span class="sp"></span>
    <button id="bm-light" class="on">Map</button><button id="bm-sat">Satellite</button>
  </header>
  <aside id="layers"></aside>
  <div id="map"></div>
  <aside id="info"><div class="hint"><b>Click anywhere on the map</b>Every drone rule at that spot appears here, most restrictive first, with what it means.</div></aside>
</div>
<script>
const DATA = __DATA__;
const META = __META__;

const EFFECTS = {
  prohibited:             { label:"No-fly",               color:"#d62828", rank:0 },
  restricted:             { label:"Restricted when active", color:"#ef7d00", rank:1 },
  authorization_required: { label:"Authorization needed",  color:"#d99a00", rank:2 },
  caution:                { label:"Advisory",              color:"#3f6fc4", rank:3 },
  allowed:                { label:"Drone-friendly",        color:"#1f9254", rank:4 },
};
const LAANC_RAMP = [[0,"#e4572e","0 ft"],[100,"#f3a712","≤100"],[200,"#f6d55c","≤200"],[300,"#c9e27a","≤300"],[400,"#8fd18b","400"]];
const laancColor = c => { for (const [v,col] of LAANC_RAMP) if ((c ?? 0) <= v) return col; return "#8fd18b"; };

function timeStatus(p) {
  const s = p.window_start ? Date.parse(p.window_start) : null, e = p.window_end ? Date.parse(p.window_end) : null, n = Date.now();
  if (!s && !e) return { k:"perm", label:"Always in effect" };
  if (e && e < n) return { k:"ended", label:"Ended" };
  if (s && s > n) return { k:"upcoming", label:"Starts " + rel(s - n) };
  return { k:"active", label:"Active now" + (e ? " · ends " + rel(e - n) : "") };
}
function rel(ms) {
  const m = Math.round(ms / 60000);
  if (m < 60) return "in " + m + " min";
  const h = Math.round(m / 60); if (h < 48) return "in " + h + " h";
  return "in " + Math.round(h / 24) + " days";
}
const fmtDate = s => s ? new Date(s).toLocaleString([], { dateStyle:"medium", timeStyle:"short" }) : "";
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));

const L_DEFS = [
  { id:"tfr_active", group:"No-fly", title:"Temporary flight restrictions (TFR)", on:true,
    desc:"VIP visits, disasters, wildfires, space launches, security events",
    what:"A temporary no-fly zone published by NOTAM. Drones may not fly inside while it is in effect unless the FAA specifically authorizes it (for example a Special Governmental Interest waiver). The altitudes below show the vertical extent.",
    style:p => ({ color:"#9b1c1c", weight:1.6, fillColor:"#d62828", fillOpacity:timeStatus(p).k==="active"?0.38:0.18 }) },
  { id:"notam_tfr", group:"No-fly", title:"TFRs from NOTAM API", on:true, desc:"VIP, disaster and space-launch TFR NOTAMs",
    what:"A temporary flight restriction taken from the FAA NOTAM system. Drones may not fly inside while it is in effect without FAA authorization. Read the NOTAM text for exact limits.",
    style:p => ({ color:"#9b1c1c", weight:1.4, dashArray:"4 3", fillColor:"#d62828", fillOpacity:0.18 }) },
  { id:"prohibited_areas", group:"No-fly", title:"Prohibited areas", on:true, desc:"No aircraft at all (e.g. P-56 White House)",
    what:"Charted prohibited airspace. No aircraft of any kind, including drones, may fly here.",
    style:p => ({ color:"#7f1010", weight:1.8, fillColor:"#b91c1c", fillOpacity:0.4 }) },
  { id:"nsufr", group:"No-fly", title:"National security sites", on:true, desc:"Military & federal facilities, 24/7 drone ban",
    what:"A national-security UAS flight restriction (14 CFR 99.7). Drone flights are prohibited at all times over this facility. Violators can face civil penalties, criminal charges and having the drone intercepted. Only the facility/FAA can grant an exception.",
    style:p => ({ color:"#7f1010", weight:1, fillColor:"#c0392b", fillOpacity:0.45 }) },
  { id:"nsufr_part_time", group:"No-fly", title:"Part-time security sites", on:true, desc:"Drone ban only when activated",
    what:"A national-security UAS restriction that applies only while it is activated (see the times below or check NOTAMs). When active, drones are prohibited.",
    style:p => ({ color:"#7f1010", weight:1, dashArray:"5 3", fillColor:"#c0392b", fillOpacity:0.25 }) },
  { id:"nda_tfr", group:"No-fly", title:"National Defense Airspace", on:true, desc:"Activated by NOTAM",
    what:"National Defense Airspace that becomes a TFR when activated by NOTAM. When active, drones are prohibited. Times and altitudes come from the activating NOTAM.",
    style:p => ({ color:"#7f1010", weight:1.2, dashArray:"6 3", fillColor:"#c0392b", fillOpacity:0.2 }) },
  { id:"seams", group:"Stadiums", title:"Stadium event TFRs (schedule)", on:true, desc:"Exact game/race times from FAA SEAMS",
    what:"A stadium TFR for a specific scheduled event: no drones within 3 NM of the venue, up to 3,000 ft above ground, from 1 hour before until 1 hour after the event. The time window below already includes that buffer.",
    style:p => { const k = timeStatus(p).k; return { color:"#b42318", weight:k==="active"?2:1, fillColor:"#d62828",
      fillOpacity:k==="active"?0.4:0, opacity:k==="ended"?0:0.8, dashArray:k==="active"?null:"2 4" }; } },
  { id:"stadiums_3nm", group:"Stadiums", title:"Stadium 3 NM rings", on:true, desc:"Only restricted during qualifying events",
    what:"A major stadium (MLB, NFL, NCAA Division I football, major motorsports). During qualifying events, drones are banned within 3 NM up to 3,000 ft above ground, from 1 hour before to 1 hour after. Outside event times this ring has no effect. See the stadium event entries for the actual schedule.",
    style:p => ({ color:"#b42318", weight:1.2, dashArray:"6 4", fillColor:"#d62828", fillOpacity:0.06 }) },
  { id:"special_use_airspace", group:"Military airspace", title:"Restricted areas & MOAs", on:true, desc:"Military training/hazard airspace",
    what:null, // depends on type, see explain()
    style:p => p.effect==="restricted" ? { color:"#c2410c", weight:1.3, fillColor:"#ef7d00", fillOpacity:0.18 }
                                        : { color:"#7c5cc4", weight:1, dashArray:"5 4", fillOpacity:0, opacity:0.55 } },
  { id:"laanc_grid", group:"Near airports", title:"LAANC grid (max altitude)", on:true, desc:"Controlled airspace: authorization needed", ramp:true,
    what:"Controlled airspace near an airport. You need FAA authorization before flying here, for both Part 107 and recreational pilots. LAANC (through apps like Aloft or Autel) can approve you instantly up to the altitude shown. A 0 ft square means no automatic approval: apply through FAADroneZone instead.",
    style:p => ({ stroke:false, fillColor:laancColor(p.ceiling_ft), fillOpacity:0.42 }) },
  { id:"class_airspace", group:"Near airports", title:"Controlled airspace boundaries", on:true, desc:"Class B / C / D / E-surface outlines",
    what:null,
    style:p => p.effect==="authorization_required" ? { color:"#a16207", weight:1.4, fillOpacity:0 }
                                                    : { color:"#3f6fc4", weight:0.6, dashArray:"2 5", fillOpacity:0, opacity:0.22 } },
  { id:"fria", group:"Drone-friendly", title:"FRIAs (no Remote ID needed)", on:true, desc:"FAA-recognized identification areas",
    what:"An FAA-Recognized Identification Area, usually a model aircraft club field. Drones without Remote ID may fly here within visual line of sight. Other rules (airspace, altitude) still apply.",
    style:p => ({ color:"#166534", weight:1, fillColor:"#1f9254", fillOpacity:0.35 }) },
  { id:"recreational_fixed_sites", group:"Drone-friendly", title:"Recreational fixed sites", on:true, desc:"Flying fields with FAA agreements",
    what:"A recreational flying site with an FAA agreement. Recreational flyers may fly here inside controlled airspace up to the altitude shown, following the site's rules.",
    style:p => ({ color:"#166534", weight:1, fillColor:"#1f9254", fillOpacity:0.2 }) },
];
const DEF = Object.fromEntries(L_DEFS.map(d => [d.id, d]));

function explain(layer, p) {
  const d = DEF[layer];
  if (d.what) return d.what;
  if (layer === "special_use_airspace")
    return p.effect === "restricted"
      ? "A military restricted area (artillery, missile testing, etc.). While it is active, drones need permission from the controlling agency. Check the times of use in the notes."
      : "Military training airspace (MOA, warning or alert area). Drones aren't banned, but military aircraft may fly low and fast here. Stay alert and check if it is active.";
  if (layer === "class_airspace")
    return p.effect === "authorization_required"
      ? "Controlled airspace that reaches the ground (Class B, C, D or E surface area). You need FAA authorization (LAANC or FAADroneZone) to fly here. The LAANC grid shows the altitude you can get approved."
      : "Airspace that usually starts above drone altitudes (e.g. Class E starting at 700 ft) or a Mode C area. It normally doesn't affect drones flying under 400 ft above ground.";
  return "";
}

function fmtAlt(ft, ref) {
  if (ref === "UNL") return "Unlimited";
  if (ref === "NOTAM") return "set by NOTAM";
  if (ft === null || ft === undefined) return "not specified";
  if (ref === "FL") return "FL" + String(Math.round(ft / 100)).padStart(3, "0") + " (" + Math.round(ft).toLocaleString() + " ft)";
  if (ft === 0 && ref === "AGL") return "Surface";
  return Math.round(ft).toLocaleString() + " ft " + (ref || "");
}
function altText(p) {
  if (p.floor_ft == null && p.ceiling_ft == null && !p.ceiling_ref) return "Not specified (see notes)";
  const top = (p.fields || {}).UPPER_DESC === "AA" ? "up to (not incl.) 18,000 ft MSL" : fmtAlt(p.ceiling_ft, p.ceiling_ref);
  return fmtAlt(p.floor_ft, p.floor_ref) + " → " + top;
}
function hoursText(p) {
  const f = p.fields || {};
  if (f.WKHR_CODE === "H24") return "Continuous (H24)";
  if (f.WKHR_RMK || f.TIMESOFUSE) return "Part-time: " + (f.WKHR_RMK || f.TIMESOFUSE);
  return "Always in effect";
}

// ---------------------------------------------------------------- map
const bounds = META.bbox ? L.latLngBounds([META.bbox[1], META.bbox[0]], [META.bbox[3], META.bbox[2]]) : null;
const map = L.map("map", { preferCanvas:true, zoomControl:true });
const renderer = L.canvas({ padding:0.4 });
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/";
map.createPane("labels"); map.getPane("labels").style.zIndex = 450; map.getPane("labels").style.pointerEvents = "none";
const light = L.layerGroup([
  L.tileLayer(ESRI + "Canvas/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}", { maxNativeZoom:16, maxZoom:19, attribution:"Basemap &copy; Esri, HERE, Garmin, OpenStreetMap contributors" }),
  L.tileLayer(ESRI + "Canvas/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}", { maxNativeZoom:16, maxZoom:19, pane:"labels" })]).addTo(map);
const sat = L.layerGroup([
  L.tileLayer(ESRI + "World_Imagery/MapServer/tile/{z}/{y}/{x}", { maxZoom:19, attribution:"Imagery &copy; Esri, Maxar, Earthstar Geographics" }),
  L.tileLayer(ESRI + "Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", { maxZoom:19, pane:"labels" })]);
document.getElementById("bm-light").onclick = () => { map.removeLayer(sat); light.addTo(map);
  document.getElementById("bm-light").classList.add("on"); document.getElementById("bm-sat").classList.remove("on"); };
document.getElementById("bm-sat").onclick = () => { map.removeLayer(light); sat.addTo(map);
  document.getElementById("bm-sat").classList.add("on"); document.getElementById("bm-light").classList.remove("on"); };

const leafletLayers = {};
let extent = null;
for (const d of L_DEFS) {
  const feats = DATA[d.id] || [];
  for (const f of feats) f._bb = bbox(f.geometry);
  const lyr = L.geoJSON(feats, { renderer, interactive:false, style:f => d.style(f.properties) });
  leafletLayers[d.id] = lyr;
  if (d.on && feats.length) { lyr.addTo(map); extent = extent ? extent.extend(lyr.getBounds()) : L.latLngBounds(lyr.getBounds()); }
}
map.fitBounds(bounds || extent || [[24, -125], [50, -66]]);
const hl = L.geoJSON(null, { renderer, interactive:false, style:{ color:"#111827", weight:3, fillOpacity:0 } }).addTo(map);
let marker = null;

function bbox(g) {
  let x0=Infinity, y0=Infinity, x1=-Infinity, y1=-Infinity;
  const walk = c => { if (typeof c[0] === "number") { if (c[0]<x0) x0=c[0]; if (c[0]>x1) x1=c[0]; if (c[1]<y0) y0=c[1]; if (c[1]>y1) y1=c[1]; } else c.forEach(walk); };
  walk(g.coordinates); return [x0, y0, x1, y1];
}
function inRing(x, y, r) {
  let inside = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const xi = r[i][0], yi = r[i][1], xj = r[j][0], yj = r[j][1];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
const inPoly = (x, y, rings) => inRing(x, y, rings[0]) && !rings.slice(1).some(h => inRing(x, y, h));
function contains(f, x, y) {
  const b = f._bb; if (x < b[0] || x > b[2] || y < b[1] || y > b[3]) return false;
  const g = f.geometry;
  return g.type === "Polygon" ? inPoly(x, y, g.coordinates) : g.type === "MultiPolygon" ? g.coordinates.some(p => inPoly(x, y, p)) : false;
}

// ---------------------------------------------------------------- layer panel
const panel = document.getElementById("layers");
let lastGroup = null, html = "";
for (const d of L_DEFS) {
  const n = (DATA[d.id] || []).length;
  if (!(d.id in DATA)) continue;
  if (d.group !== lastGroup) { html += `<div class="grp">${esc(d.group)}</div>`; lastGroup = d.group; }
  const s = d.style((DATA[d.id] || [{ properties:{ effect:"restricted", ceiling_ft:100 } }])[0]?.properties || {});
  const fill = s.fillOpacity ? s.fillColor : "transparent", stroke = s.stroke === false ? s.fillColor : s.color;
  html += `<label class="lyr ${n ? "" : "empty"}"><input type="checkbox" data-id="${d.id}" ${d.on && n ? "checked" : ""} ${n ? "" : "disabled"}>
    <span class="sw" style="background:${fill};border-color:${stroke}"></span>
    <span><div class="t">${esc(d.title)}</div><div class="d">${esc(d.desc)}</div>
    ${d.ramp ? `<div class="ramp">${LAANC_RAMP.map(([v, c, t]) => `<span style="background:${c}">${t}</span>`).join("")}</div>` : ""}</span>
    <span class="n">${n.toLocaleString()}</span></label>`;
}
const okRows = META.manifest.filter(r => r.status === "ok");
const newest = okRows.map(r => r.fetched_utc).sort().pop();
html += `<div class="fresh">Data downloaded ${esc(fmtDate(newest))} from FAA public sources (UAS Data Delivery System, SEAMS, tfr.faa.gov).
  ${META.manifest.filter(r => r.status !== "ok").map(r => `<br>${esc(r.layer)}: ${esc(r.status)}`).join("")}
  <br><br>Reference map only. Always confirm with an FAA-approved app (B4UFLY / LAANC) before flying.</div>`;
panel.innerHTML = html;
panel.querySelectorAll("input[type=checkbox]").forEach(cb => cb.addEventListener("change", () => {
  const l = leafletLayers[cb.dataset.id]; cb.checked ? l.addTo(map) : map.removeLayer(l); hl.bringToFront();
}));
document.getElementById("sub").textContent = newest ? "Data as of " + fmtDate(newest) : "";

// ---------------------------------------------------------------- click → every rule here
map.on("click", e => {
  const x = e.latlng.lng, y = e.latlng.lat, hits = [];
  for (let i = L_DEFS.length - 1; i >= 0; i--) {
    const d = L_DEFS[i];
    for (const f of DATA[d.id] || []) {
      if (d.id === "seams" && timeStatus(f.properties).k === "ended") continue;
      if (contains(f, x, y)) hits.push({ layer:d.id, f });
    }
  }
  hits.sort((a, b) => (EFFECTS[a.f.properties.effect]?.rank ?? 9) - (EFFECTS[b.f.properties.effect]?.rank ?? 9));
  hl.clearLayers(); hits.filter(h => h.layer !== "seams").forEach(h => hl.addData(h.f));
  if (marker) marker.remove();
  marker = L.circleMarker(e.latlng, { radius:6, color:"#111827", weight:2, fillColor:"#fff", fillOpacity:1 }).addTo(map);
  renderInfo(hits, e.latlng);
});

function renderInfo(hits, ll) {
  const info = document.getElementById("info");
  let h = "";
  if (!hits.length) {
    h = `<div class="verdict" style="background:#1f9254"><div class="big">No mapped restrictions here</div>
      <div class="small">Uncontrolled airspace in this dataset: Part 107 rules still apply (400 ft AGL, visual line of sight, etc.).</div></div>`;
  } else {
    const worst = EFFECTS[hits[0].f.properties.effect] || EFFECTS.caution;
    const counts = {};
    hits.forEach(x => { const k = x.f.properties.effect; counts[k] = (counts[k] || 0) + 1; });
    const laanc = hits.filter(x => x.layer === "laanc_grid").map(x => x.f.properties.ceiling_ft).filter(v => v != null);
    const activeNow = hits.filter(x => x.f.properties.effect === "prohibited" && ["active", "perm"].includes(timeStatus(x.f.properties).k)
                                       && x.layer !== "stadiums_3nm").length;
    let headline = worst.label;
    if (hits[0].f.properties.effect === "prohibited") headline = activeNow ? "No-fly zone — in effect now" : "No-fly zone at scheduled times";
    h += `<div class="verdict" style="background:${worst.color}"><div class="big">${esc(headline)}</div>
      <div class="small">${Object.entries(counts).map(([k, n]) => `${n} × ${esc(EFFECTS[k]?.label || k)}`).join(" · ")}
      ${laanc.length ? (Math.min(...laanc) > 0 ? `<br>LAANC can approve up to <b>${Math.min(...laanc)} ft AGL</b> here`
                                               : `<br><b>No instant LAANC approval here (0 ft grid)</b>: apply via FAADroneZone`) : ""}</div></div>`;
  }
  h += `<div class="coord">${ll.lat.toFixed(5)}, ${ll.lng.toFixed(5)} · showing all layers, including hidden ones</div>`;

  const seams = hits.filter(x => x.layer === "seams");
  let seamsDone = false;
  for (const { layer, f } of hits) {
    if (layer === "seams") { if (!seamsDone) { h += seamsCard(seams.map(s => s.f)); seamsDone = true; } continue; }
    h += card(layer, f.properties);
  }
  info.innerHTML = h;
  info.scrollTop = 0;
}

function seamsCard(fs) {
  fs.sort((a, b) => Date.parse(a.properties.window_start) - Date.parse(b.properties.window_start));
  const p0 = fs[0].properties, eff = EFFECTS.prohibited;
  const rows = fs.slice(0, 25).map(f => { const p = f.properties, st = timeStatus(p);
    return `<div class="ev"><span>${esc(p.name)}</span><span style="white-space:nowrap">${esc(fmtDate(p.window_start))}<span class="st ${st.k}">${esc(st.k === "active" ? "NOW" : st.k === "upcoming" ? "" : st.label)}</span></span></div>`; }).join("");
  return `<div class="card" style="--c:${eff.color}">
    <div class="card-h"><span class="badge">${eff.label} during events</span><span class="lt">${esc(DEF.seams.title)}</span></div>
    <div class="nm">${fs.length} scheduled event${fs.length > 1 ? "s" : ""} here</div>
    <div class="what">${esc(DEF.seams.what)}</div>
    <dl><dt>Altitude</dt><dd>${esc(altText(p0))}</dd><dt>Rule</dt><dd>${esc(p0.citation)}</dd>
    <dt>Source</dt><dd>FAA SEAMS, updated ${esc(fmtDate(p0.source_last_edit))}</dd></dl>
    <details open><summary>Event schedule (TFR start, your local time)</summary>${rows}${fs.length > 25 ? `<div class="ev">…and ${fs.length - 25} more</div>` : ""}</details></div>`;
}

function card(layer, p) {
  const d = DEF[layer], eff = EFFECTS[p.effect] || EFFECTS.caution, st = timeStatus(p);
  let when;
  if (layer === "stadiums_3nm") when = "Only during qualifying events (1 hr before → 1 hr after)";
  else if (st.k === "perm") when = esc(hoursText(p));
  else when = `${p.window_start ? esc(fmtDate(p.window_start)) : "now"} → ${p.window_end ? esc(fmtDate(p.window_end)) : "until further notice"}`;
  const fields = Object.entries(p.fields || {});
  const notes = p.notes || "";
  return `<div class="card" style="--c:${eff.color}">
    <div class="card-h"><span class="badge">${esc(eff.label)}</span><span class="lt">${esc(d.title)}</span></div>
    <div class="nm">${esc(p.name || "(unnamed)")}</div>
    <div class="what">${esc(explain(layer, p))}</div>
    <dl>
      <dt>Altitude</dt><dd>${layer === "laanc_grid"
        ? (p.ceiling_ft > 0 ? `LAANC instant approval up to <b>${p.ceiling_ft} ft AGL</b>` : "<b>0 ft</b>: no instant LAANC approval, apply via FAADroneZone")
        : esc(altText(p))}</dd>
      <dt>When</dt><dd>${when}${st.k !== "perm" && layer !== "stadiums_3nm" ? `<span class="st ${st.k}">${esc(st.label)}</span>` : ""}</dd>
      <dt>Rule</dt><dd>${esc(p.citation || "—")}</dd>
      <dt>Source</dt><dd>${p.source_url && p.source_url.includes("tfr.faa.gov") ? `<a href="${esc(p.source_url)}" target="_blank">tfr.faa.gov</a>` : "FAA"} · updated ${esc(fmtDate(p.source_last_edit) || "—")}</dd>
    </dl>
    ${notes ? (notes.length < 220 ? `<details open><summary>Notes</summary><p>${esc(notes)}</p></details>`
                                  : `<details><summary>Notes / full text</summary><p>${esc(notes)}</p></details>`) : ""}
    ${fields.length ? `<details><summary>All FAA fields (${fields.length})</summary><table class="f">${fields.map(([k, v]) =>
        `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("")}</table></details>` : ""}
  </div>`;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
