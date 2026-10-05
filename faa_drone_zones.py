"""
faa_drone_zones.py
Download FAA drone-restriction layers from the FAA UAS Data Delivery System
(public ArcGIS FeatureServers) and save them locally.

Output (folder: faa_drone_zones/):
  - one GeoJSON file per layer (always)
  - stadium_3nm_circles.geojson  (needs geopandas)
  - faa_drone_zones.gpkg         (needs geopandas; one layer per dataset + circles)

Setup:  pip install requests geopandas      (Mac: pip3)
Run:    python faa_drone_zones.py            (Mac: python3)
"""

import json
import sys
import time
from pathlib import Path

import requests

try:
    import geopandas as gpd
    HAVE_GPD = True
except ImportError:
    HAVE_GPD = False

BASE = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"

# output name -> FeatureServer service name
LAYERS = {
    "stadiums": "Stadiums",
    "national_security_uas_restrictions": "DoD_Mar_13",
    "part_time_national_security_uas_restrictions": "Part_Time_National_Security_UAS_Flight_Restrictions",
    "national_defense_airspace_tfr": "National_Defense_Airspace_TFR_Areas",
    "prohibited_areas": "Prohibited_Areas",
    "faa_recognized_identification_areas": "FAA_Recognized_Identification_Areas",
    "recreational_flyer_fixed_sites": "Recreational_Flyer_Fixed_Sites",
}

STADIUM_RADIUS_NM = 3
METERS_PER_NM = 1852
PAGE_SIZE = 1000
OUT_DIR = Path(__file__).resolve().parent / "faa_drone_zones"


def fetch_layer(service, page_size=PAGE_SIZE):
    """Fetch every feature from layer 0 of a FeatureServer as a GeoJSON dict, paging as needed."""
    url = f"{BASE}/{service}/FeatureServer/0/query"
    features, offset = [], 0
    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": 4326,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        for attempt in range(4):
            try:
                r = requests.get(url, params=params, timeout=60)
                r.raise_for_status()
                data = r.json()
            except (requests.RequestException, ValueError):
                if attempt == 3:
                    raise
                time.sleep(5 * (attempt + 1))
                continue
            # ArcGIS reports rate limiting inside a 200 response; quota resets each minute
            if data.get("error", {}).get("code") == 429 and attempt < 3:
                print("(rate-limited, waiting 60s)", end=" ", flush=True)
                time.sleep(61)
                continue
            break
        if "error" in data:
            raise RuntimeError(f"{service}: {data['error']}")
        batch = data.get("features", [])
        features.extend(batch)
        exceeded = data.get("properties", {}).get("exceededTransferLimit", False)
        if not batch or (not exceeded and len(batch) < page_size):
            break
        offset += len(batch)
    return {"type": "FeatureCollection", "features": features}


def stadium_circles(stadiums_gdf, radius_nm=STADIUM_RADIUS_NM):
    """Buffer each stadium point by radius_nm, using a local equidistant projection per point
    so circles are true-distance everywhere (CONUS, Alaska, Hawaii, territories)."""
    radius_m = radius_nm * METERS_PER_NM
    circles = []
    for pt in stadiums_gdf.geometry:
        aeqd = f"+proj=aeqd +lat_0={pt.y} +lon_0={pt.x} +datum=WGS84 +units=m"
        local = gpd.GeoSeries([pt], crs=4326).to_crs(aeqd)
        circles.append(local.buffer(radius_m, 32).to_crs(4326).iloc[0])
    out = stadiums_gdf.copy()
    out["radius_nm"] = radius_nm
    out["note"] = "Stadium TFR: 3 NM / 3,000 ft AGL, 1 hr before to 1 hr after qualifying events"
    return out.set_geometry(gpd.GeoSeries(circles, crs=4326))


def main():
    OUT_DIR.mkdir(exist_ok=True)
    gpkg = OUT_DIR / "faa_drone_zones.gpkg"
    if HAVE_GPD and gpkg.exists():
        gpkg.unlink()
    if not HAVE_GPD:
        print("geopandas not installed: writing GeoJSON only (no GeoPackage, no stadium circles).\n")

    failed = []
    for name, service in LAYERS.items():
        print(f"Downloading {name} ...", end=" ", flush=True)
        try:
            fc = fetch_layer(service)
        except Exception as e:
            print(f"FAILED ({e})")
            failed.append(name)
            continue
        path = OUT_DIR / f"{name}.geojson"
        path.write_text(json.dumps(fc), encoding="utf-8")
        print(f"{len(fc['features'])} features -> {path.name}")

        if HAVE_GPD and fc["features"]:
            gdf = gpd.GeoDataFrame.from_features(fc["features"], crs=4326)
            gdf.to_file(gpkg, layer=name, driver="GPKG")
            if name == "stadiums":
                circles = stadium_circles(gdf)
                circles.to_file(OUT_DIR / "stadium_3nm_circles.geojson", driver="GeoJSON")
                circles.to_file(gpkg, layer="stadium_3nm_circles", driver="GPKG")
                print(f"  + {len(circles)} stadium {STADIUM_RADIUS_NM} NM circles")

    print(f"\nDone. Files are in: {OUT_DIR}")
    if HAVE_GPD:
        print(f"GeoPackage: {gpkg.name}")
    if failed:
        print(f"Failed layers: {', '.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
