r"""
osm_point_footprints.py - replace OSM facilities mapped as a single point with the OSM building
footprint they sit in (or the nearest one within 25 m).

Many schools (and some hospitals) are tagged on a point in OpenStreetMap, while the building is
drawn separately as a polygon. This asks Overpass, per state and category, for buildings within
60 m of the points, then matches locally:
  - building polygon containing the point (smallest one if nested)  -> geometry_source = osm_building_footprint
  - else nearest building edge within 25 m                          -> geometry_source = osm_building_nearest
  - else the point is kept                                          -> geometry_source = osm_point
Footprints larger than 250,000 m2 (malls, whole campuses) are not used.

Run after osm_infra_pull.py:   .venv\Scripts\python osm_point_footprints.py
Rewrites out/osm/<STATE>_<category>.geojson in place (raw Overpass responses are untouched) and
writes out/osm/footprint_summary.csv. Safe to re-run: already-matched features are skipped.
"""

import argparse
import csv
import json
import math
import time
from pathlib import Path

from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.strtree import STRtree

import osm_infra_pull as oip

HERE = Path(__file__).resolve().parent
QUERY = """[out:json][timeout:300];
node(id:{ids})->.p;
way(around.p:60)["building"];
out tags geom;
"""
CHUNK = 100                  # small batches: 400 timed out (504) on a busy server
MAX_AREA_M2 = 250_000


def m_per_deg(lat):
    return 111_320 * math.cos(math.radians(lat)), 110_540


def area_m2(poly):
    mx, my = m_per_deg(poly.centroid.y)
    return poly.area * mx * my


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(HERE / "out" / "osm"))
    ap.add_argument("--categories", nargs="+", default=["school", "hospital", "power_plant", "substation", "college_university"])
    args = ap.parse_args()
    outdir = Path(args.outdir)
    summary = []

    for f in sorted(outdir.glob("*_*.geojson")):
        state, cat = f.stem.split("_", 1)
        if cat not in args.categories:
            continue
        fc = json.loads(f.read_text(encoding="utf-8"))
        todo = [ft for ft in fc["features"]
                if ft["geometry"]["type"] == "Point" and ft["properties"].get("osm_type") == "node"
                and not ft["properties"].get("possible_duplicate_of") and not ft["properties"].get("geometry_source")]
        row = {"state": state, "category": cat, "points": len(todo), "footprint": 0, "nearest": 0, "kept_point": 0, "status": "ok"}
        if not todo:
            summary.append(row)
            continue
        print(f"{state} {cat}: {len(todo)} points")
        polys, meta = [], []
        try:
            for k in range(0, len(todo), CHUNK):
                ids = ",".join(str(ft["properties"]["osm_id"]) for ft in todo[k:k + CHUNK])
                data = oip.run_query(QUERY.format(ids=ids), oip.ENDPOINTS)
                for el in data.get("elements", []):
                    pts = [(p["lon"], p["lat"]) for p in el.get("geometry", []) if p]
                    if len(pts) >= 4 and pts[0] == pts[-1]:
                        pg = Polygon(pts)
                        pg = pg if pg.is_valid else pg.buffer(0)
                        if not pg.is_empty and area_m2(pg) <= MAX_AREA_M2:
                            polys.append(pg)
                            meta.append({"id": el["id"], "building": el.get("tags", {}).get("building")})
                print(f"   chunk {k // CHUNK + 1}: {len(polys)} buildings so far")
                time.sleep(oip.PAUSE_BETWEEN_QUERIES_S)
        except RuntimeError as e:
            row["status"] = f"error: {str(e)[:200]}"
            print(f"   ERROR: {e}")
            summary.append(row)
            continue

        tree = STRtree(polys) if polys else None
        for ft in todo:
            pt = Point(ft["geometry"]["coordinates"])
            props, best = ft["properties"], None
            if tree is not None:
                inside = [i for i in tree.query(pt, predicate="within")]
                if inside:
                    best = (min(inside, key=lambda i: polys[i].area), "osm_building_footprint")
                else:
                    mx, my = m_per_deg(pt.y)
                    near = tree.query(pt.buffer(25 / my))          # ~25 m search box
                    if len(near):
                        i = min(near, key=lambda i: polys[i].exterior.distance(pt))
                        dx = polys[i].exterior.distance(pt) * (mx + my) / 2
                        if dx <= 25:
                            best = (i, "osm_building_nearest")
            if best:
                i, src = best
                ft["geometry"] = mapping(polys[i])
                props.update(geometry_source=src, footprint_osm_url=f"https://www.openstreetmap.org/way/{meta[i]['id']}",
                             geometry_type=polys[i].geom_type)
                row["footprint" if src == "osm_building_footprint" else "nearest"] += 1
            else:
                props["geometry_source"] = "osm_point"
                row["kept_point"] += 1
        for ft in fc["features"]:                                   # areas mapped directly in OSM
            ft["properties"].setdefault("geometry_source", "osm_area" if ft["geometry"]["type"] != "Point" else "osm_point")
        f.write_text(json.dumps(fc), encoding="utf-8")
        print(f"   matched {row['footprint']} inside + {row['nearest']} nearest; kept {row['kept_point']} as points")
        summary.append(row)

    with open(outdir / "footprint_summary.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0]) if summary else ["state"])
        w.writeheader()
        w.writerows(summary)
    failed = [f"{r['state']} {r['category']}" for r in summary if r["status"] != "ok"]
    print("\nDone." + (f" Failed: {', '.join(failed)} (re-run to retry)" if failed else ""))


if __name__ == "__main__":
    main()
