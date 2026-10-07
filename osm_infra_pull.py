#!/usr/bin/env python3
"""
osm_infra_pull.py
Pull OpenStreetMap infrastructure for selected US states via the Overpass API.

Categories: power plants, substations, hospitals, schools (K-12),
optionally colleges/universities.

Outputs (default ./out/osm):
  <STATE>_<category>.geojson   one file per state and category, full geometry
  osm_infra_all.csv            one row per feature (centroid, key tags, no geometry)
  summary.csv                  counts by state, category, and geometry type
  raw/<STATE>_<category>.json  raw Overpass responses, for reproducibility

Requirements: Python 3.10+
  pip install requests shapely

Usage:
  python osm_infra_pull.py                          # NY, KY, VA, all four categories
  python osm_infra_pull.py --states VA --categories substation hospital
  python osm_infra_pull.py --higher-ed              # also pull colleges/universities

Data (c) OpenStreetMap contributors, ODbL 1.0. Attribution is required on the map.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from shapely.geometry import (LineString, Point, Polygon,
                              mapping)
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

STATES = {
    "NY": "US-NY",
    "KY": "US-KY",
    "VA": "US-VA",
}
# State names: some ISO3166-2 area lookups return nothing (seen for US-KY, Oct 2026),
# so the query matches the state by ISO code OR by name.
STATE_NAMES = {"NY": "New York", "KY": "Kentucky", "VA": "Virginia"}

# Each category is a list of Overpass selectors (combined with OR).
CATEGORIES = {
    "power_plant": ['nwr["power"="plant"]'],
    "substation": ['nwr["power"="substation"]'],
    "hospital": ['nwr["amenity"="hospital"]', 'nwr["healthcare"="hospital"]'],
    "school": ['nwr["amenity"="school"]'],
}
HIGHER_ED = {
    "college_university": ['nwr["amenity"="college"]',
                           'nwr["amenity"="university"]'],
}

# Tags copied into the CSV/GeoJSON properties when present.
KEEP_TAGS = [
    "name", "operator", "owner",
    # power
    "substation", "voltage", "plant:source", "plant:output:electricity",
    "plant:method",
    # health
    "healthcare", "emergency", "healthcare:speciality", "beds",
    # schools
    "isced:level", "school", "grades", "operator:type",
    # address
    "addr:housenumber", "addr:street", "addr:city", "addr:postcode",
    "addr:state",
]

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

QUERY_TEMPLATE = """
[out:json][timeout:300][maxsize:536870912];
(
  area["ISO3166-2"="{iso}"]["admin_level"="4"];
  area["name"="{name}"]["admin_level"="4"]["boundary"="administrative"];
)->.s;
(
{selectors}
);
out meta geom;
"""

PAUSE_BETWEEN_QUERIES_S = 15   # be polite to the public servers
MAX_RETRIES = 8
MAX_DATA_AGE_DAYS = 7          # reject responses from mirrors with stale data (seen: kumi.systems 3 months old)


# --------------------------------------------------------------------------
# Overpass
# --------------------------------------------------------------------------

def build_query(iso: str, selectors: list[str], name: str = "") -> str:
    body = "\n".join(f"  {sel}(area.s);" for sel in selectors)
    return QUERY_TEMPLATE.format(iso=iso, name=name, selectors=body)


def run_query(query: str, endpoints: list[str]) -> dict:
    last_err = None
    for attempt in range(MAX_RETRIES):
        endpoint = endpoints[attempt % len(endpoints)]
        try:
            # Overpass rejects (504) anonymous-looking clients; identify the project
            r = requests.post(endpoint, data={"data": query}, timeout=400,
                              headers={"User-Agent": "uas-restriction-map/0.1 (research; contact via github.com/nicovacca)",
                                       "Accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                remark = data.get("remark", "")
                if "runtime error" in remark.lower():
                    raise RuntimeError(f"Overpass runtime error: {remark}")
                base = data.get("osm3s", {}).get("timestamp_areas_base") or data.get("osm3s", {}).get("timestamp_osm_base")
                if base:
                    age = datetime.now(timezone.utc) - datetime.fromisoformat(base.replace("Z", "+00:00"))
                    if age.days > MAX_DATA_AGE_DAYS:
                        raise RuntimeError(f"stale data from {endpoint} (areas as of {base})")
                return data
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
        except (requests.RequestException, ValueError, RuntimeError) as e:
            last_err = str(e)
        wait = min(60 * (attempt + 1), 300)
        print(f"    attempt {attempt + 1} failed ({str(last_err)[:120]}); "
              f"retrying in {wait}s", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"Query failed after {MAX_RETRIES} attempts: {last_err}")


# --------------------------------------------------------------------------
# Geometry conversion
# --------------------------------------------------------------------------

def _coords(geom_list: list[dict]) -> list[tuple[float, float]]:
    return [(p["lon"], p["lat"]) for p in geom_list if p]


def way_geometry(el: dict):
    pts = _coords(el.get("geometry", []))
    if len(pts) >= 4 and pts[0] == pts[-1]:
        poly = Polygon(pts)
        return poly if poly.is_valid else poly.buffer(0)
    if len(pts) >= 2:
        return LineString(pts)
    if len(pts) == 1:
        return Point(pts[0])
    return None


def _lines_to_polygons(lines: list[LineString]) -> list[Polygon]:
    """Node the member ways together and build closed rings from them."""
    merged = unary_union(lines)
    seq = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    return list(polygonize(seq))


def relation_geometry(el: dict):
    """Assemble multipolygon relations from member way geometries.
    Falls back to the union of member geometries for other relation types."""
    outers, inners, others = [], [], []
    for m in el.get("members", []):
        g = None
        if m.get("type") == "way" and m.get("geometry"):
            pts = _coords(m["geometry"])
            if len(pts) >= 2:
                g = LineString(pts)
        elif m.get("type") == "node" and "lat" in m:
            g = Point(m["lon"], m["lat"])
        if g is None:
            continue
        role = m.get("role", "")
        if isinstance(g, LineString) and role in ("outer", ""):
            outers.append(g)
        elif isinstance(g, LineString) and role == "inner":
            inners.append(g)
        else:
            others.append(g)

    if outers:
        outer_polys = _lines_to_polygons(outers)
        if outer_polys:
            shape = unary_union(outer_polys)
            if inners:
                inner_polys = _lines_to_polygons(inners)
                if inner_polys:
                    shape = shape.difference(unary_union(inner_polys))
            return shape if shape.is_valid else shape.buffer(0)

    members = outers + inners + others
    return unary_union(members) if members else None


def element_geometry(el: dict):
    t = el["type"]
    if t == "node":
        return Point(el["lon"], el["lat"])
    if t == "way":
        return way_geometry(el)
    if t == "relation":
        return relation_geometry(el)
    return None


# --------------------------------------------------------------------------
# Feature building
# --------------------------------------------------------------------------

def to_features(data: dict, state: str, category: str,
                retrieved_at: str) -> list[dict]:
    feats = []
    for el in data.get("elements", []):
        if el["type"] not in ("node", "way", "relation"):
            continue
        tags = el.get("tags", {})
        geom = element_geometry(el)
        if geom is None or geom.is_empty:
            continue
        c = geom.representative_point() if geom.geom_type != "Point" else geom
        props = {
            "state": state,
            "category": category,
            "osm_type": el["type"],
            "osm_id": el["id"],
            "osm_url": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
            "osm_version": el.get("version"),
            "osm_last_edit": el.get("timestamp"),
            "geometry_type": geom.geom_type,
            "centroid_lat": round(c.y, 7),
            "centroid_lon": round(c.x, 7),
            "possible_duplicate_of": "",
            "source": "OpenStreetMap",
            "license": "ODbL-1.0",
            "retrieved_at": retrieved_at,
        }
        for k in KEEP_TAGS:
            props[k] = tags.get(k, "")
        feats.append({"type": "Feature", "geometry": geom, "properties": props})
    return feats


def flag_point_duplicates(feats: list[dict]) -> int:
    """A facility is often mapped twice: once as a point and once as an area.
    Flag points that fall inside an area of the same category."""
    areas = [f for f in feats
             if f["geometry"].geom_type in ("Polygon", "MultiPolygon")]
    if not areas:
        return 0
    tree = STRtree([f["geometry"] for f in areas])
    flagged = 0
    for f in feats:
        if f["geometry"].geom_type != "Point":
            continue
        for idx in tree.query(f["geometry"], predicate="within"):
            a = areas[int(idx)]["properties"]
            f["properties"]["possible_duplicate_of"] = f"{a['osm_type']}/{a['osm_id']}"
            flagged += 1
            break
    return flagged


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def write_geojson(feats: list[dict], path: Path) -> None:
    fc = {
        "type": "FeatureCollection",
        "attribution": "(c) OpenStreetMap contributors, ODbL 1.0",
        "features": [
            {"type": "Feature", "geometry": mapping(f["geometry"]),
             "properties": f["properties"]}
            for f in feats
        ],
    }
    path.write_text(json.dumps(fc), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--states", nargs="+", default=list(STATES),
                    choices=list(STATES), help="State codes to pull")
    ap.add_argument("--categories", nargs="+", default=list(CATEGORIES),
                    help="Categories to pull (default: all)")
    ap.add_argument("--higher-ed", action="store_true",
                    help="Also pull colleges and universities")
    ap.add_argument("--outdir", default="out/osm")
    ap.add_argument("--endpoint", action="append",
                    help="Override Overpass endpoint(s)")
    ap.add_argument("--resume", action="store_true",
                    help="skip jobs whose raw response already exists (rebuilds outputs from it)")
    args = ap.parse_args()

    cats = {k: v for k, v in CATEGORIES.items() if k in args.categories}
    if args.higher_ed:
        cats.update(HIGHER_ED)
    unknown = set(args.categories) - set(CATEGORIES)
    if unknown:
        sys.exit(f"Unknown categories: {', '.join(sorted(unknown))}")

    endpoints = args.endpoint or ENDPOINTS
    outdir = Path(args.outdir)
    (outdir / "raw").mkdir(parents=True, exist_ok=True)

    all_rows, summary = [], []
    jobs = [(s, c) for s in args.states for c in cats]

    for i, (state, cat) in enumerate(jobs, 1):
        print(f"[{i}/{len(jobs)}] {state} {cat} ...")
        raw = outdir / "raw" / f"{state}_{cat}.json"
        fresh = True
        if args.resume and raw.exists():
            data = json.loads(raw.read_text(encoding="utf-8"))
            retrieved_at, fresh = data.get("_retrieved_at", ""), False
            print("    (from saved raw response)")
        else:
            query = build_query(STATES[state], cats[cat], STATE_NAMES[state])
            retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                data = run_query(query, endpoints)
            except RuntimeError as e:
                print(f"    ERROR: {e}", file=sys.stderr)
                summary.append({"state": state, "category": cat, "features": "", "points": "", "polygons": "",
                                "other": "", "possible_point_duplicates": "", "retrieved_at": retrieved_at,
                                "status": f"error: {str(e)[:200]}"})
                continue
            data["_retrieved_at"] = retrieved_at
            raw.write_text(json.dumps(data), encoding="utf-8")

        feats = to_features(data, state, cat, retrieved_at)
        dups = flag_point_duplicates(feats)
        write_geojson(feats, outdir / f"{state}_{cat}.geojson")

        counts: dict[str, int] = {}
        for f in feats:
            gt = f["properties"]["geometry_type"]
            counts[gt] = counts.get(gt, 0) + 1
            all_rows.append(f["properties"])
        summary.append({
            "state": state, "category": cat, "features": len(feats),
            "points": counts.get("Point", 0),
            "polygons": counts.get("Polygon", 0) + counts.get("MultiPolygon", 0),
            "other": sum(v for k, v in counts.items()
                         if k not in ("Point", "Polygon", "MultiPolygon")),
            "possible_point_duplicates": dups,
            "retrieved_at": retrieved_at,
            "status": "ok",
        })
        if not feats:
            print(f"    WARNING: 0 features for {state} {cat}; check that the state area was found", file=sys.stderr)
        print(f"    {len(feats)} features "
              f"({summary[-1]['polygons']} areas, {summary[-1]['points']} points, "
              f"{dups} possible duplicates)")
        if i < len(jobs) and fresh:
            time.sleep(PAUSE_BETWEEN_QUERIES_S)

    if all_rows:
        with open(outdir / "osm_infra_all.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(all_rows[0]))
            w.writeheader()
            w.writerows(all_rows)
    with open(outdir / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0]) if summary else ["state"])
        w.writeheader()
        w.writerows(summary)

    failed = [f"{r['state']} {r['category']}" for r in summary if r.get('status') != 'ok']
    print(f"\nDone. Files written to {outdir.resolve()}")
    print("Remember: map must show '(c) OpenStreetMap contributors'.")
    if failed:
        print(f"Failed jobs: {', '.join(failed)}. Re-run with --resume to retry only those.")


if __name__ == "__main__":
    main()
