r"""
make_v2_full.py - build the AirHub-style map (uas_app_map.html) with per-state data.

Input:  out_full/uas_rules.gpkg   (python build_uas_layers.py --out out_full)
Output: v2_full/index.html + v2_full/data/states/<ST>.js   -> double-click v2_full\index.html

Every zone is clipped at state borders (Census TIGER state boundaries, which include coastal
waters) and written to one file per state. The map's sidebar has a States list: only checked
states are loaded and drawn, so the full U.S. stays fast. Plain <script> files, so it works
from file:// without a web server.
"""

import argparse
import json
import shutil
from pathlib import Path

import geopandas as gpd
import requests
import shapely
from shapely.geometry import shape

import make_uas_map as mm

HERE = Path(__file__).resolve().parent
FAA = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
STATES_URL = "https://www2.census.gov/geo/tiger/TIGER2023/STATE/tl_2023_us_state.zip"
CB_URL = "https://www2.census.gov/geo/tiger/GENZ2024/shp/cb_2024_us_{kind}_500k.zip"   # county, cd119


def load_states():
    cache = HERE / "_cache" / "tiger_states.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    print("Downloading Census TIGER state boundaries...")
    s = gpd.read_file(STATES_URL).to_crs(4326)[["STUSPS", "NAME", "geometry"]]
    s["geometry"] = s.geometry.simplify(2e-4, preserve_topology=True)   # ~20 m
    cache.parent.mkdir(exist_ok=True)
    s.to_file(cache, driver="GPKG")
    return s


def load_cb(kind, cols):
    """Census 2024 cartographic boundaries (counties / 119th Congress districts), cached, simplified."""
    cache = HERE / "_cache" / f"cb_{kind}.gpkg"
    if cache.exists():
        return gpd.read_file(cache)
    print(f"Downloading Census {kind} boundaries...")
    g = gpd.read_file(CB_URL.format(kind=kind)).to_crs(4326)[cols + ["geometry"]]
    g["geometry"] = g.geometry.simplify(3e-4, preserve_topology=True)   # ~30 m
    cache.parent.mkdir(exist_ok=True)
    g.to_file(cache, driver="GPKG")
    return g


def polygons_only(g):
    """Keep the polygon parts of a clip result (drops slivers turned into lines/points)."""
    if g is None or g.is_empty:
        return None
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g
    parts = [p for p in shapely.get_parts(g) if p.geom_type in ("Polygon", "MultiPolygon")]
    return shapely.union_all(parts) if parts else None


def state_bbox(geom):
    x0, y0, x1, y1 = geom.bounds
    if x1 - x0 > 180:   # Alaska crosses the antimeridian: use the western-hemisphere parts
        west = [p for p in shapely.get_parts(geom) if p.bounds[2] < 0]
        x0, y0, x1, y1 = shapely.union_all(west).bounds
    return [round(v, 4) for v in (x0, y0, x1, y1)]


def fetch_airports():
    url, out, offset = f"{FAA}/US_Airport/FeatureServer/0/query", [], 0
    while True:
        r = requests.get(url, timeout=120, params={
            "where": "OPERSTATUS IS NULL OR OPERSTATUS <> 'CLOSED'",
            "outFields": "IDENT,ICAO_ID,NAME,TYPE_CODE,PRIVATEUSE,MIL_CODE,SERVCITY,STATE,ELEVATION",
            "outSR": 4326, "f": "json", "resultOffset": offset, "resultRecordCount": 1000}).json()
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


def main():
    ap = argparse.ArgumentParser(description="Build the AirHub-style map with a per-state toggle")
    ap.add_argument("--gpkg", default=str(HERE / "out_full" / "uas_rules.gpkg"))
    ap.add_argument("--out", default=str(HERE / "v2_full"))
    ap.add_argument("--title", default="v2_full – U.S. Drone Airspace")
    ap.add_argument("--states", default="NY", help="states checked on first open, e.g. NY,NJ")
    args = ap.parse_args()

    gpkg, out = Path(args.gpkg), Path(args.out)
    shutil.rmtree(out / "data", ignore_errors=True)
    (out / "data" / "states").mkdir(parents=True)
    js = lambda o: json.dumps(o, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")

    states = load_states()
    st_geoms = list(states.geometry)
    per_state = {code: {"layers": {}, "airports": []} for code in states.STUSPS}

    print(f"Reading {gpkg}")
    data = mm.load_layers(gpkg)
    print("Clipping zones at state borders...")
    for layer, feats in data.items():
        if not feats:
            continue
        geoms = [shape(f["geometry"]) for f in feats]
        tree = shapely.STRtree(geoms)
        n = 0
        for code, sg in zip(states.STUSPS, st_geoms):
            idx = tree.query(sg, predicate="intersects")
            if not len(idx):
                continue
            clipped = shapely.intersection(shapely.make_valid([geoms[i] for i in idx]), sg)
            bucket = per_state[code]["layers"].setdefault(layer, [])
            for i, g in zip(idx, clipped):
                g = polygons_only(g)
                if g is None:
                    continue
                gi = g.__geo_interface__
                bucket.append({"type": "Feature", "properties": feats[i]["properties"],
                               "geometry": {"type": gi["type"], "coordinates": mm.rnd(gi["coordinates"])}})
                n += 1
        print(f"  {layer:<26}{len(feats):>7} -> {n:>7} state pieces")

    print("Fetching FAA airports...")
    apts = fetch_airports()
    pts = shapely.points([a["x"] for a in apts], [a["y"] for a in apts])
    pt_tree = shapely.STRtree(pts)
    for code, sg in zip(states.STUSPS, st_geoms):
        for i in pt_tree.query(sg, predicate="contains"):
            per_state[code]["airports"].append(apts[i])

    counties = load_cb("county", ["GEOID", "STATEFP", "STUSPS", "NAMELSAD"])
    fips = dict(zip(counties.STATEFP, counties.STUSPS))
    districts = load_cb("cd119", ["GEOID", "STATEFP", "CD119FP"])
    for code in per_state:
        per_state[code]["counties"], per_state[code]["districts"] = [], []
    for r in counties.itertuples(index=False):
        if r.STUSPS in per_state:
            gi = r.geometry.__geo_interface__
            per_state[r.STUSPS]["counties"].append({"id": "c" + r.GEOID, "name": r.NAMELSAD, "type": gi["type"],
                                                    "coords": mm.rnd(gi["coordinates"], 4)})
    for r in districts.itertuples(index=False):
        code = fips.get(r.STATEFP)
        if code in per_state and r.geometry is not None:
            gi = r.geometry.__geo_interface__
            label = f"{code}-AL (at large)" if r.CD119FP in ("00", "98") else f"{code}-{int(r.CD119FP)}"
            per_state[code]["districts"].append({"id": "d" + r.GEOID, "name": label, "type": gi["type"],
                                                 "coords": mm.rnd(gi["coordinates"], 4)})
    for code in per_state:
        per_state[code]["counties"].sort(key=lambda a: a["name"])
        per_state[code]["districts"].sort(key=lambda a: (len(a["name"]), a["name"]))

    # OpenStreetMap infrastructure (osm_infra_pull.py output), informational only
    osm_dir = HERE / "out" / "osm"
    keep = ["name", "operator", "voltage", "substation", "plant:source", "plant:output:electricity", "emergency",
            "healthcare", "beds", "isced:level", "grades", "operator:type", "addr:city"]
    for f in sorted(osm_dir.glob("*_*.geojson")) if osm_dir.exists() else []:
        code, cat = f.stem.split("_", 1)
        if code not in per_state:
            continue
        out_feats = []
        for ft in json.loads(f.read_text(encoding="utf-8")).get("features", []):
            pr, g = ft.get("properties") or {}, ft.get("geometry")
            if not g or pr.get("possible_duplicate_of"):
                continue                    # skip points already covered by an area of the same facility
            if g["type"] not in ("Polygon", "MultiPolygon", "Point"):
                g = {"type": "Point", "coordinates": [pr.get("centroid_lon"), pr.get("centroid_lat")]}
            props = {k: pr.get(k) for k in keep if pr.get(k)}
            props.update(osm_url=pr.get("osm_url"), osm_last_edit=pr.get("osm_last_edit"))
            out_feats.append({"type": "Feature", "properties": props,
                              "geometry": {"type": g["type"], "coordinates": mm.rnd(g["coordinates"])}})
        per_state[code].setdefault("osm", {})[cat] = out_feats
        print(f"  OSM {code} {cat:<20}{len(out_feats):>6}")

    index = []
    for code, name, sg in zip(states.STUSPS, states.NAME, st_geoms):
        payload = per_state[code]
        payload["outline"] = mm.rnd(shapely.simplify(sg, 0.005).__geo_interface__["coordinates"], 4)
        payload["outline_type"] = shapely.simplify(sg, 0.005).geom_type
        f = out / "data" / "states" / f"{code}.js"
        f.write_text(f"(window.UAS_STATE_DATA=window.UAS_STATE_DATA||{{}})[{js(code)}]={js(payload)};", encoding="utf-8")
        index.append({"code": code, "name": name, "bbox": state_bbox(sg), "mb": round(f.stat().st_size / 1e6, 1)})
    index.sort(key=lambda s: s["name"])

    defaults = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    boxes = [s["bbox"] for s in index if s["code"] in defaults] or [[-125.0, 24.0, -66.5, 49.5]]
    view = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    manifest = mm.load_manifest(gpkg.with_name("manifest.csv"))
    (out / "data" / "states.js").write_text(
        f"window.UAS_STATES={js(index)};window.UAS_META={js({'bbox': view, 'manifest': manifest, 'defaultStates': defaults})};",
        encoding="utf-8")

    html = (HERE / "uas_app_map.html").read_text(encoding="utf-8")
    # data comes from data/states/*.js at runtime: fill the template placeholders
    html = html.replace("__DATA__", "{}").replace("__AIRPORTS__", "[]").replace("__META__", "window.UAS_META")
    i = html.index("<script>\nlet DATA")
    html = html[:i] + '<script src="data/states.js"></script>\n' + html[i:]
    html = html.replace('<meta name="viewport"', f"<title>{args.title}</title>\n<meta name=\"viewport\"", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    total = sum(s["mb"] for s in index)
    print(f"\nWrote {out / 'index.html'}  ({len(index)} state files, {total:.0f} MB total, {len(apts):,} airports)")


if __name__ == "__main__":
    main()
