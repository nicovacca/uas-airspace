"""
build_uas_layers.py
Build one GeoPackage of U.S. drone (UAS) airspace rules from public FAA sources,
with every layer normalized to the same columns so rules can be stacked in QGIS.

Setup:   pip install requests geopandas            (Mac: pip3)
Test:    python build_uas_layers.py --bbox -77.6,38.5,-76.9,39.1
Full US: python build_uas_layers.py
Refresh: python build_uas_layers.py --group fast    (every 5 min: SEAMS, TFR list, NOTAMs)
         python build_uas_layers.py --group daily   (security restriction layers)
         python build_uas_layers.py --group weekly  (everything else)

Output (out/ next to this script, or --out):
  uas_rules.gpkg   one layer per source (needs geopandas)
  geojson/*.geojson  same layers as GeoJSON (written when geopandas is missing, or with --geojson)
  manifest.csv     per layer: status (ok/error/skipped), count, server last-edit date, ...

NOTAM layer: register at api.faa.gov and set FAA_NOTAM_CLIENT_ID / FAA_NOTAM_CLIENT_SECRET.
Without them that layer is reported as "skipped"; every other layer is public.

Columns on every feature:
  layer, source_id, name
  effect        prohibited | restricted | authorization_required | caution | allowed
  floor_ft, floor_ref, ceiling_ft, ceiling_ref   (ref: AGL | MSL | FL | UNL)
  window_start, window_end   ISO UTC; empty = permanent / not time-limited
  notes, citation
  rule_case     which labeling rule produced `effect` (see make_rule_review.py; decisions in rule_decisions.json)
  source_url, source_last_edit, fetched_utc, source_attrs (original FAA attributes as JSON)
"""

import argparse
import csv
import datetime as dt
import json
import math
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

try:
    import geopandas as gpd
    import pandas as pd
    import pyogrio
    HAVE_GPD = True
except ImportError:
    HAVE_GPD = False

FAA = "https://services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services"
SEAMS_URL = "https://services1.arcgis.com/n4Ot9Qz0t5espY4s/arcgis/rest/services/SEAMS_Production_View/FeatureServer/0"
TFR_BASE = "https://tfr.faa.gov"
TFR_WFS = (TFR_BASE + "/geoserver/TFR/ows?service=WFS&version=1.1.0&request=GetFeature"
           "&typeName=TFR:V_TFR_LOC&outputFormat=application/json&srsname=EPSG:4326")
NOTAM_API = "https://external-api.faa.gov/notamapi/v1/notams"

COLUMNS = ["layer", "source_id", "name", "effect", "floor_ft", "floor_ref", "ceiling_ft", "ceiling_ref",
           "window_start", "window_end", "schedule", "notes", "citation", "rule_case",
           "source_url", "source_last_edit", "fetched_utc", "source_attrs"]
NUMERIC = {"floor_ft", "ceiling_ft"}
MANIFEST_COLS = ["layer", "group", "status", "count", "server_last_edit", "fetched_utc",
                 "bbox", "seconds", "source", "message"]

STADIUM_NM = 3
METERS_PER_NM = 1852.0
STADIUM_CITATION = "14 CFR 99.7; Pub. L. 108-7 Sec. 352 (stadium TFR)"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "build_uas_layers.py (research; python-requests)"


class Skip(Exception):
    """Layer intentionally not built (e.g. missing API keys)."""


# ----------------------------------------------------------------------------- HTTP

def http_get(url, params=None, headers=None, timeout=120, as_json=False, retries=4):
    """GET with retries. Waits out ArcGIS per-minute quota (429) instead of failing.
    Returns the response, or the parsed JSON body when as_json=True."""
    net_fail = rate_fail = 0
    while True:
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException:
            net_fail += 1
            if net_fail > retries:
                raise
            time.sleep(5 * net_fail)
            continue
        throttled, body = r.status_code == 429, None
        if as_json and r.ok:
            try:
                body = r.json()
            except ValueError:
                net_fail += 1
                if net_fail > 4:
                    raise
                time.sleep(5 * net_fail)
                continue
            # ArcGIS reports quota errors inside an HTTP 200 body
            throttled = isinstance(body, dict) and (body.get("error") or {}).get("code") == 429
        if throttled:
            rate_fail += 1
            if rate_fail > 15:
                r.raise_for_status()
                raise RuntimeError("rate limit: still throttled after 15 minutes")
            print(" (rate-limited, waiting 60s)", end="", flush=True)
            time.sleep(61)
            continue
        if r.status_code >= 500 and net_fail < retries:
            net_fail += 1
            time.sleep(5 * net_fail)
            continue
        r.raise_for_status()
        return body if as_json else r


def get_json(url, params=None, headers=None, retries=4):
    data = http_get(url, params, headers, as_json=True, retries=retries)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"server error: {data['error']}")
    return data


# ----------------------------------------------------------------------------- helpers

def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(value):
    """Epoch ms / datetime / string -> ISO UTC string (or None)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def num(value):
    try:
        v = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def adds_alt(val, uom, code, desc=None):
    """FAA ADDS altitude fields (VAL/UOM/CODE/DESC) -> (feet, ref)."""
    code, desc = (code or "").upper(), (desc or "").upper()
    if code == "UNLTD":
        return None, "UNL"
    if code == "BYNOTAM":
        return None, "NOTAM"
    v = num(val)
    if v is not None and v <= -9000 and desc == "AA":
        return 18000.0, "MSL"            # -9998 + AA = up to but not including Class A (18,000 ft MSL)
    if v is None or v <= -9000:          # other -9998 sentinels: not specified
        return None, None
    if (uom or "").upper() == "FL" or code == "STD":
        return v * 100, "FL"
    if code == "SFC":
        return v, "AGL"
    return v, "MSL" if code == "MSL" else (code or None)


def text_alt(s):
    """Strings like "Surface", "2,500' AGL", "400 ft MSL" -> (feet, ref)."""
    if not s:
        return None, None
    s = str(s).strip()
    if s.lower().startswith(("surface", "sfc", "ground")):
        return 0.0, "AGL"
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:'|ft|feet)?\s*(AGL|MSL)?", s, re.I)
    if not m:
        return None, None
    return num(m.group(1)), (m.group(2) or "").upper() or None


def circle(lon, lat, radius_m, n=64):
    """Geodesic circle polygon (spherical earth) as GeoJSON geometry."""
    R = 6371008.8
    lat1, lon1, d = math.radians(lat), math.radians(lon), radius_m / R
    ring = []
    for i in range(n + 1):
        b = 2 * math.pi * (i % n) / n
        lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
        lon2 = lon1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1),
                                 math.cos(d) - math.sin(lat1) * math.sin(lat2))
        ring.append([round(math.degrees(lon2), 7), round(math.degrees(lat2), 7)])
    return {"type": "Polygon", "coordinates": [ring]}


def geom_bounds(geom):
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1])
        else:
            for x in c:
                walk(x)
    if geom.get("type") == "GeometryCollection":
        for g in geom.get("geometries", []):
            b = geom_bounds(g)
            if b:
                xs += [b[0], b[2]]; ys += [b[1], b[3]]
    else:
        walk(geom.get("coordinates") or [])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def in_bbox(geom, bbox):
    if not bbox:
        return True
    b = geom and geom_bounds(geom)
    return bool(b) and not (b[2] < bbox[0] or b[0] > bbox[2] or b[3] < bbox[1] or b[1] > bbox[3])


# Reviewed labeling decisions (written by the rule review page). They override the default
# effect / citation for a rule_case, so a decision is made once and applied on every build.
DECISIONS_FILE = Path(__file__).resolve().parent / "rule_decisions.json"
_DEC_RAW = json.loads(DECISIONS_FILE.read_text(encoding="utf-8")) if DECISIONS_FILE.exists() else {}
DECISIONS = {k: {**v, "effect": v.get("decision") or v.get("effect")}
             for k, v in (_DEC_RAW.get("decisions", _DEC_RAW)).items() if isinstance(v, dict)}
EXCLUDE_NOTAMS = set(_DEC_RAW.get("exclude_notams", []))
OVERRIDES = _DEC_RAW.get("overrides", [])    # e.g. force the Ceiba, PR TFR to prohibited     # e.g. 4/9383 (DC speed restriction, not a drone ban)

FAA_REGION_NAME = re.compile(r"^A(AL|CE|EA|GL|NE|NM|SO|SW|WP)\b")   # FAA region prefix, e.g. "AWP CQ D SAIPAN ISLAND"
FOREIGN_NAME = re.compile(r"\b(ACC|TORONTO|VANCOUVER|MONTREAL)\b")


def class_is_us(p):
    """U.S.-designation test for Class_Airspace records (final review 2026-10-06).
    Keep U.S. class types (CLASS_A..CLASS_E6, MODE C) that are not known foreign names. Region-prefixed
    CTR/TMA records with a D/E2-E6 token (Saipan, Midway) and state-suffixed CTLZ records (Houlton ME,
    International Falls MN) duplicate proper U.S. class records already in the data, so they are dropped.
    Point Roberts (ANM BC C ...) is kept: Canadian-controlled Class C over U.S. soil."""
    n, lt = p.get("NAME") or "", p.get("LOCAL_TYPE") or ""
    if FAA_REGION_NAME.match(n):
        return not re.search(r"\b(E[2-6]|D)\b", n)
    return (lt.startswith("CLASS_") or lt == "MODE C") and not FOREIGN_NAME.search(n)


def sua_is_us(p):
    """Special use / prohibited airspace: the FAA record's COUNTRY field (Canadian records are dropped)."""
    c = (p.get("COUNTRY") or "").upper()
    return c in ("UNITED STATES", "") and not (p.get("NAME") or "").startswith("(MY)")


def rule(geom, layer, source_id, name, effect, floor=(None, None), ceiling=(None, None),
         window=(None, None), notes=None, citation=None, case=None, source_url=None, attrs=None, schedule=None):
    d = DECISIONS.get(case) or {}
    effect, citation = d.get("effect") or effect, d.get("citation") or citation
    if d.get("note"):
        notes = join(notes, d["note"])
    for o in OVERRIDES:
        if o.get("layer") == layer and (o.get("name_contains") or "\0").upper() in (name or "").upper():
            effect, notes = o.get("decision") or effect, join(notes, o.get("note"))
    return {"type": "Feature", "geometry": geom, "properties": {
        "layer": layer, "source_id": None if source_id is None else str(source_id), "name": name,
        "effect": effect, "floor_ft": floor[0], "floor_ref": floor[1],
        "ceiling_ft": ceiling[0], "ceiling_ref": ceiling[1],
        "window_start": window[0], "window_end": window[1],
        "schedule": json.dumps(schedule) if schedule else None,
        "notes": notes, "citation": citation, "rule_case": case, "source_url": source_url,
        "source_attrs": json.dumps(attrs, default=str) if attrs is not None else None}}


def join(*parts, sep="; "):
    parts = [str(p) for p in parts if p not in (None, "", "None")]
    return sep.join(p.rstrip(".") if sep == "; " and i < len(parts) - 1 else p for i, p in enumerate(parts))


# ----------------------------------------------------------------------------- ArcGIS

def arcgis_meta(url):
    meta = get_json(url, {"f": "json"})
    ei = meta.get("editingInfo") or {}
    return iso(ei.get("dataLastEditDate") or ei.get("lastEditDate")), int(meta.get("maxRecordCount") or 1000)


def arcgis_pages(url, bbox, out_fields="*", page_size=1000):
    """Yield lists of GeoJSON features from an ArcGIS FeatureServer layer, paging until done."""
    params = {"where": "1=1", "outFields": out_fields, "outSR": 4326, "f": "geojson",
              "geometryPrecision": 6, "resultRecordCount": page_size}
    if bbox:
        params.update(geometry=",".join(map(str, bbox)), geometryType="esriGeometryEnvelope",
                      inSR=4326, spatialRel="esriSpatialRelIntersects")
    offset = 0
    while True:
        params["resultOffset"] = offset
        try:
            data = get_json(f"{url}/query", params, retries=1)
        except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as e:
            # big polygon pages make the server time out (504): retry the same offset with smaller pages
            status = getattr(getattr(e, "response", None), "status_code", None)
            if (status is None or status >= 500) and params["resultRecordCount"] > 10:
                params["resultRecordCount"] = max(10, params["resultRecordCount"] // 2)
                print(f" (server slow, page size -> {params['resultRecordCount']})", end="", flush=True)
                continue
            raise
        feats = [f for f in data.get("features", []) if f.get("geometry")]
        if data.get("features"):
            yield feats
        exceeded = (data.get("properties") or {}).get("exceededTransferLimit") or data.get("exceededTransferLimit")
        if not data.get("features") or not exceeded:
            break
        offset += len(data["features"])


def arcgis_source(service, to_rule, out_fields="*", page_size=2000):
    """Build a layer-source generator for an FAA ArcGIS service (layer 0)."""
    url = service if service.startswith("http") else f"{FAA}/{service}/FeatureServer/0"

    def source(ctx):
        ctx.source = url
        ctx.last_edit, max_rec = arcgis_meta(url)
        for feats in arcgis_pages(url, ctx.bbox, out_fields, page_size=min(max_rec, page_size)):
            yield [r for r in (to_rule(f["geometry"], f.get("properties") or {}, url) for f in feats) if r]
    source.to_rule = to_rule          # lets --renormalize re-label stored data without downloading
    return source


# ----------------------------------------------------------------------------- layer mappings

def r_laanc(g, p, url):
    c = num(p.get("CEILING"))
    apt = join(p.get("APT1_NAME"), p.get("APT1_FAAID") and f"({p.get('APT1_FAAID')})", sep=" ")
    note = (f"Controlled airspace near {apt or 'airport'}. Max altitude FAA pre-approves via LAANC: {c:g} ft AGL."
            if c else f"Controlled airspace near {apt or 'airport'}. 0 ft grid: no LAANC auto-approval; "
                      "apply through FAADroneZone.")
    return rule(g, "laanc_grid", p.get("GLOBALID") or p.get("OBJECTID"), apt or "UAS facility map grid",
                "authorization_required", (0.0, "AGL"), (c, "AGL"), notes=join(note, p.get("AIRSPACE_1")),
                citation="14 CFR 107.41", case="LAANC:CEILING>0" if c else "LAANC:CEILING=0", source_url=url, attrs=p)


SURFACE_CLASSES = {"CLASS_B", "CLASS_C", "CLASS_D", "CLASS_E2", "CLASS_E3", "CLASS_E4"}


def r_class(g, p, url):
    if not class_is_us(p):
        return None
    lt, cls = p.get("LOCAL_TYPE") or "", p.get("CLASS") or ""
    floor = adds_alt(p.get("LOWER_VAL"), p.get("LOWER_UOM"), p.get("LOWER_CODE"))
    ceil = adds_alt(p.get("UPPER_VAL"), p.get("UPPER_UOM"), p.get("UPPER_CODE"), p.get("UPPER_DESC"))
    shelf = lt in ("CLASS_B", "CLASS_C") and (floor[0] or 0) > 0
    if shelf:
        effect = "caution"
        note = (f"Class {cls} shelf starting at {floor[0]:,.0f} ft {floor[1] or ''}. The airspace below the shelf is not "
                f"Class {cls}; see the LAANC grid for what applies at drone altitudes.")
    elif lt in SURFACE_CLASSES or (cls in ("B", "C", "D") and p.get("TYPE_CODE") in ("CLASS", "CTR")):
        effect = "authorization_required"
        note = f"Class {cls} controlled airspace: authorization (LAANC or FAADroneZone) needed to fly inside it."
    else:
        effect = "caution"
        note = f"{lt or p.get('TYPE_CODE')} (class {cls or 'n/a'}): normally above drone altitudes or advisory only."
    return rule(g, "class_airspace", p.get("GLOBAL_ID"), p.get("NAME"), effect, floor, ceil,
                notes=join(note, "Ceiling: up to but not including 18,000 ft MSL (base of Class A)"
                           if (p.get("UPPER_DESC") or "").upper() == "AA" else None,
                           p.get("WKHR_RMK") and f"Hours: {p.get('WKHR_RMK')}"),
                citation="14 CFR 107.41; 14 CFR Part 71", case=f"CLASS:{lt or p.get('TYPE_CODE') or '?'}" + (f"_{cls}" if not lt and cls else "") + (":SHELF" if shelf else ""), source_url=url, attrs=p)


SUA_TYPES = {
    "P": ("prohibited", "Prohibited area", "14 CFR 73.83"),
    "R": ("restricted", "Restricted area: no flight while active without controlling agency approval", "14 CFR 73.13"),
    "MOA": ("caution", "Military operations area", "FAA Order JO 7400.10"),
    "W": ("caution", "Warning area (offshore hazard)", "FAA Order JO 7400.10"),
    "A": ("caution", "Alert area (high volume of training)", "FAA Order JO 7400.10"),
    "D": ("caution", "Danger area", "FAA Order JO 7400.10"),
}


def r_sua(g, p, url, layer="special_use_airspace"):
    if not sua_is_us(p):
        return None
    if layer == "special_use_airspace" and p.get("TYPE_CODE") == "P":
        return None                               # same 13 areas as the Prohibited_Areas layer
    floor = adds_alt(p.get("LOWER_VAL"), p.get("LOWER_UOM"), p.get("LOWER_CODE"))
    elevated = p.get("TYPE_CODE") == "R" and (floor[0] or 0) > 0
    effect, label, cite = SUA_TYPES.get(p.get("TYPE_CODE"), ("caution", f"SUA type {p.get('TYPE_CODE')}", "14 CFR Part 73"))
    if elevated:
        label += f". Begins at {floor[0]:,.0f} ft {floor[1] or ''}; the airspace below the floor is not part of it"
    return rule(g, layer, p.get("GLOBAL_ID"), p.get("NAME"), effect,
                floor,
                adds_alt(p.get("UPPER_VAL"), p.get("UPPER_UOM"), p.get("UPPER_CODE"), p.get("UPPER_DESC")),
                notes=join(label, p.get("TIMESOFUSE") and f"Times of use: {p.get('TIMESOFUSE')}",
                           p.get("CONT_AGENT") and f"Controlling agency: {p.get('CONT_AGENT')}", p.get("REMARKS")),
                citation=cite + ("; 14 CFR 99.7" if p.get("TYPE_CODE") == "P" else ""),
                case=f"{'PROHIBITED' if layer == 'prohibited_areas' else 'SUA'}:{p.get('TYPE_CODE')}" + (":ELEVATED_FLOOR" if elevated else ""),
                source_url=url, attrs=p, schedule=parse_times_of_use(p.get("TIMESOFUSE"), p.get("GMTOFFSET"), p.get("DST_CODE")))


DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _days(txt):
    if re.search(r"\bDAILY\b", txt):
        return list(range(7))
    out = set()
    for a, b in re.findall(r"\b(MON|TUE|WED|THU|FRI|SAT|SUN)\s*-\s*(MON|TUE|WED|THU|FRI|SAT|SUN)\b", txt):
        i, j = DAYS.index(a), DAYS.index(b)
        out.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
    out.update(DAYS.index(x) for x in re.findall(r"\b(MON|TUE|WED|THU|FRI|SAT|SUN)\b(?!\s*-)", txt) if not re.search(rf"-\s*{x}\b", txt))
    return sorted(out) or list(range(7))


def parse_times_of_use(text, gmt_offset=None, dst=None):
    """FAA TIMESOFUSE text -> {"type": continuous|notam|scheduled, "windows": [{"days": [0-6], "start": "HHMM",
    "end": "HHMM"}], "other_times_notam": bool, "utc_offset": h, "dst": bool, "raw": text}. Local times.
    SUNRISE/SUNSET are approximated as 0600/1800 ("approx_sun": true)."""
    if not text:
        return None
    t = " ".join(str(text).upper().replace("%", " ").split())
    base = {"raw": text, "utc_offset": num(gmt_offset), "dst": str(dst) == "1"}
    if t.startswith("CONTINUOUS") or t == "H24":
        return {**base, "type": "continuous"}
    wins, approx = [], False
    for seg in re.split(r";", t):
        for a, b in re.findall(r"(\d{4}|SUNRISE|SR)\s*-\s*(\d{4}|SUNSET|SS)", seg):
            approx |= not (a.isdigit() and b.isdigit())
            a = a if a.isdigit() else "0600"
            b = b if b.isdigit() else "1800"
            wins.append({"days": _days(seg), "start": a, "end": b})
    notam = "NOTAM" in t
    if not wins:
        return {**base, "type": "notam" if notam else "unparsed"}
    return {**base, "type": "scheduled", "windows": wins, "other_times_notam": notam, "approx_sun": approx}


def r_prohibited(g, p, url):
    return r_sua(g, {**p, "TYPE_CODE": "P"}, url, layer="prohibited_areas")


def r_stadium(g, p, url):
    if g.get("type") != "Point":
        return None
    lon, lat = g["coordinates"][:2]
    return rule(circle(lon, lat, STADIUM_NM * METERS_PER_NM), "stadiums_3nm", p.get("GLOBAL_ID"), p.get("NAME"),
                "prohibited", (0.0, "AGL"), (3000.0, "AGL"),
                notes=join(f"{STADIUM_NM} NM stadium TFR. Applies only from 1 hour before to 1 hour after qualifying "
                           "events (MLB, NFL, NCAA Div. I football, major motorsport). Exact times: seams layer.",
                           join(p.get("CITY"), p.get("STATE"), sep=", "), p.get("STATUS_CODE") and f"Venue status: {p.get('STATUS_CODE')}"),
                citation=STADIUM_CITATION, case="STADIUM:RING", source_url=url, attrs=p)


def r_fria(g, p, url):
    end = iso(p.get("endDate"))
    if end and end < utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"):
        return None                               # FRIA approval has expired
    return rule(g, "fria", p.get("refNumber") or p.get("ObjectId"), p.get("title"), "allowed",
                window=(iso(p.get("startDate")), iso(p.get("endDate"))),
                notes=join("FAA-Recognized Identification Area: drones without Remote ID may fly here within visual line of sight.",
                           p.get("orgName"), join(p.get("city"), p.get("state"), sep=", ")),
                citation="14 CFR 89.115(b); 14 CFR Part 89 Subpart C", case="FRIA", source_url=url, attrs=p)


def r_fixed_site(g, p, url):
    c = num(p.get("CEILING"))
    return rule(g, "recreational_fixed_sites", p.get("SITE_ID") or p.get("GLOBALID"), p.get("SITE_NAME"), "allowed",
                (0.0, "AGL"), (c, "AGL" if c is not None else None),
                notes=join("Recreational flyer fixed site in controlled airspace: flying allowed here under the site's "
                           f"agreement" + (f", up to {c:g} ft AGL" if c is not None else ""),
                           join(p.get("CITY"), p.get("STATE"), sep=", "), p.get("POC") and f"POC: {p.get('POC')}"),
                citation="49 U.S.C. 44809(a)(5)", case="FIXED_SITE", source_url=url, attrs=p)


def r_nsufr(g, p, url, layer="nsufr"):
    part_time = layer == "nsufr_part_time"
    win = (iso(p.get("ACTIVETIME")), iso(p.get("ENDTIME"))) if part_time else (None, None)
    when = ("Part-time: in effect only when activated" + ("" if win[0] else " (no activation time published; check NOTAMs)")
            if part_time else "In effect 24/7")
    return rule(g, layer, p.get("FAA_ID") or p.get("OBJECTID"), p.get("Facility") or p.get("Base"), "prohibited",
                text_alt(p.get("Floor")), text_alt(p.get("Ceiling")), win,
                notes=join(f"National security UAS flight restriction. {when}.", p.get("Base"), p.get("Branch"),
                           p.get("Reason") and f"Reason: {p.get('Reason')}", p.get("ALERTYPE"), p.get("ADVISENOTE")),
                citation="14 CFR 99.7 (special security instructions for UAS)",
                case=("NSUFR:PART_TIME:" + ("WINDOW" if win[0] else "NO_WINDOW")) if part_time else "NSUFR:FULL_TIME",
                source_url=url, attrs=p)


def r_nsufr_pt(g, p, url):
    return r_nsufr(g, p, url, layer="nsufr_part_time")


def r_nda(g, p, url):
    return rule(g, "nda_tfr", p.get("GLOBAL_ID"), p.get("NAME"), "prohibited",
                notes=join("National Defense Airspace TFR", p.get("WKHR_RMK") and f"Active: {p.get('WKHR_RMK')}",
                           "Altitudes and times are set by the activating NOTAM (see tfr_active / notam_tfr)."),
                citation="14 CFR 99.7; 49 U.S.C. 40103(b)(3)", case="NDA:BY_NOTAM", source_url=url, attrs=p)


def r_seams(g, p, url):
    return rule(g, "seams", p.get("GAME_DETAIL_ID") or p.get("GlobalID"), join(p.get("EVENT_NAME"), p.get("VENUE"), sep=" @ "),
                "prohibited", (0.0, "AGL"), (3000.0, "AGL"), (iso(p.get("GAME_DATE")), iso(p.get("END_DATE"))),
                notes=join("Stadium TFR for this event (window = TFR start/end, incl. 1 hr before/after).",
                           p.get("LEAGUE_NAME"), p.get("STATUS") and f"Status: {p.get('STATUS')}",
                           f"Active now: {'yes' if p.get('IS_ACTIVE') == 1 else 'no'}",
                           p.get("TIME_REMAINING") and f"Time remaining: {p.get('TIME_REMAINING')}"),
                citation=STADIUM_CITATION, case="SEAMS:EVENT", source_url=url, attrs=p)


# ----------------------------------------------------------------------------- TFRs (tfr.faa.gov)

LEGAL_CFR = {"SECURITY": "99.7", "UAS PUBLIC GATHERING": "99.7", "HAZARDS": "91.137", "VIP": "91.141",
             "SPACE OPERATIONS": "91.143", "AIR SHOWS/SPORTS": "91.145"}
TFR_CITES = {"91.137": "disaster/hazard area", "91.138": "disaster area in Hawaii", "91.139": "emergency air traffic rules",
             "91.141": "VIP / presidential movement", "91.143": "space operations", "91.144": "high barometric pressure",
             "91.145": "aerial demonstration or major sporting event", "99.7": "special security instructions"}


def tfr_alt(area, which):
    code, val, uom = (area.findtext(f"codeDistVer{which}"), area.findtext(f"valDistVer{which}"),
                      area.findtext(f"uomDistVer{which}"))
    v = num(val)
    if v is None:
        return None, None
    if (uom or "").upper() == "FL" or code == "STD":
        return v * 100, "FL"
    return v, {"HEI": "AGL", "ALT": "MSL"}.get(code, code)


def tfr_details(ctx, notam_id, mod_time):
    """Fetch (cached) tfr.faa.gov detail XML for one NOTAM; return parsed dict or None."""
    fname = f"detail_{notam_id.replace('/', '_')}.xml"
    cache = ctx.out / "_cache" / "tfr" / f"{notam_id.replace('/', '_')}_{mod_time}.xml"
    if cache.exists():
        raw = cache.read_bytes()
    else:
        try:
            raw = http_get(f"{TFR_BASE}/download/{fname}", timeout=60).content
        except Exception:
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    try:
        root = ET.fromstring(raw.lstrip(b"\xef\xbb\xbf"))
    except ET.ParseError:
        return None
    n = root.find(".//Not")
    areas = []
    for a in root.iter("aseTFRArea"):
        areas.append({"name": a.findtext("txtName"), "floor": tfr_alt(a, "Lower"), "ceiling": tfr_alt(a, "Upper")})
    # tfr.faa.gov XML times are UTC even when codeTimeZone says e.g. EDT (checked against NOTAM text)
    utc = lambda s: s + "Z" if s else None
    return {"start": utc(n.findtext("dateEffective") if n is not None else None),
            "end": utc(n.findtext("dateExpire") if n is not None else None),
            "issued": utc(n.findtext(".//dateIssued") if n is not None else None),
            "code": root.findtext(".//TfrNot/codeType"), "areas": areas,
            "text": root.findtext(".//txtDescrTraditional")}


def terrain_max_ft(ctx, geom, key):
    """Highest terrain (ft MSL) under a TFR polygon: grid sample (~20 x 20) via the Open-Meteo elevation API
    (Copernicus GLO-90 DEM). Cached per NOTAM. Returns None if the lookup fails."""
    from shapely.geometry import shape, Point
    cache = ctx.out / "_cache" / "terrain" / f"{key.replace('/', '_')}.json"
    if cache.exists():
        return json.loads(cache.read_text())["max_ft"]
    try:
        poly = shape(geom)
        x0, y0, x1, y1 = poly.bounds
        n = 20                               # ~400 grid points + ~100 boundary points (Open-Meteo counts each point)
        pts = [(x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * j / (n - 1)) for i in range(n) for j in range(n)]
        edge = [c for part in getattr(poly, "geoms", [poly]) for c in part.exterior.coords]
        pts = [p for p in pts if poly.contains(Point(p))] + edge[::max(1, len(edge) // 100)]
        top = None
        for k in range(0, len(pts), 100):
            chunk = pts[k:k + 100]
            r = http_get("https://api.open-meteo.com/v1/elevation", {"latitude": ",".join(f"{p[1]:.5f}" for p in chunk),
                         "longitude": ",".join(f"{p[0]:.5f}" for p in chunk)}, as_json=True)
            vals = [v for v in r.get("elevation", []) if v is not None]
            if vals:
                top = max(top or -1e9, max(vals) * 3.28084)
        if top is None:
            return None
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"max_ft": top, "samples": len(pts)}))
        return top
    except Exception:
        return None


def fmt_alt(a):
    v, ref = a
    if ref == "UNL":
        return "UNL"
    if v is None:
        return "?"
    if ref == "FL":
        return f"FL{int(v // 100):03d}"
    return "SFC" if v == 0 and ref == "AGL" else f"{v:g} ft {ref or ''}".strip()


def src_tfr(ctx):
    ctx.source = TFR_WFS
    tlist = get_json(f"{TFR_BASE}/tfrapi/getTfrList")
    mods = {t["notam_id"]: t.get("mod_abs_time") or "" for t in tlist}
    ctx.last_edit = max((m for m in mods.values() if m), default=None)
    if ctx.last_edit:
        ctx.last_edit = dt.datetime.strptime(ctx.last_edit, "%Y%m%d%H%M").strftime("%Y-%m-%dT%H:%M:00Z")
    wfs = get_json(TFR_WFS)
    shaped, out, no_detail = set(), [], 0
    for f in wfs.get("features", []):
        p, g = f.get("properties") or {}, f.get("geometry")
        nid = (p.get("NOTAM_KEY") or "").split("-")[0]
        shaped.add(nid)
        if not g or not in_bbox(g, ctx.bbox) or nid in EXCLUDE_NOTAMS:
            continue
        d = tfr_details(ctx, nid, mods.get(nid) or p.get("LAST_MODIFICATION_DATETIME") or "x")
        floor = ceiling = (None, None)
        notes, code = [p.get("TITLE"), f"Type: {p.get('LEGAL')}"], None
        window = (None, None)
        if d:
            no_areas = not d["areas"]
            code = (re.match(r"\d+\.\d+", d["code"] or "") or [None])[0]
            window = (d["start"] or d["issued"], d["end"])
            if not no_areas:
                floors = [a["floor"] for a in d["areas"] if a["floor"][0] is not None]
                ceils = [a["ceiling"] for a in d["areas"] if a["ceiling"][0] is not None or a["ceiling"][1] == "UNL"]
                floors = [(0.0, "AGL") if a[0] == 0 and a[1] in ("MSL", "FL") else a for a in floors]   # 0 MSL = surface
                floor = min(floors, key=lambda a: (a[1] != "AGL", a[0])) if floors else (None, None)
                ceiling = max(ceils, key=lambda a: float("inf") if a[1] == "UNL" else a[0]) if ceils else (None, None)
                if len(d["areas"]) > 1:
                    notes.append(f"{len(d['areas'])} areas (shape not matched to area; floor/ceiling = overall range): "
                                 + ", ".join(f"{a['name']}: {fmt_alt(a['floor'])}-{fmt_alt(a['ceiling'])}" for a in d["areas"]))
            if d["end"] is None:
                notes.append("Until further notice.")
            if d["text"]:
                notes.append(d["text"][:1500])
        else:
            no_detail += 1
        # drones fly below 400 ft AGL: a TFR whose lowest floor is well above that is caution-only
        effect = "caution" if floor[1] == "AGL" and (floor[0] or 0) > 400 else "prohibited"
        case = ("TFR:NO_DETAILS" if not d else "TFR:FLOOR_ABOVE_400_AGL" if effect == "caution"
                else "TFR:FLOOR_MSL" if floor[1] in ("MSL", "FL") else "TFR:FLOOR_NEAR_SURFACE")
        if case == "TFR:FLOOR_MSL":
            top = terrain_max_ft(ctx, g, nid)
            if top is None:
                case = "TFR:FLOOR_MSL:TERRAIN_UNCHECKED"
            else:
                clear = floor[0] - top
                notes.append(f"Terrain check: highest ground under the TFR is about {top:,.0f} ft MSL; the floor is "
                             f"{clear:,.0f} ft above it")
                if clear <= 400:
                    case = "TFR:FLOOR_MSL:NEAR_TERRAIN"
        if d and len(d["areas"]) > 1:
            case += ":MULTI_AREA"
        code = LEGAL_CFR.get((p.get("LEGAL") or "").upper(), code)      # citation follows the TFR category
        cite = f"14 CFR {code}" + (f" ({TFR_CITES[code]})" if code in TFR_CITES else "") if code else None
        out.append(rule(g, "tfr_active", p.get("NOTAM_KEY") or nid, p.get("TITLE"), effect, floor, ceiling, window,
                        notes=join(*notes), citation=cite, case=case,
                        source_url=f"{TFR_BASE}/tfr3/?page=detail_{nid.replace('/', '_')}", attrs=p))
    missing = [n for n in mods if n not in shaped]
    ctx.message = join(f"{len(missing)} TFRs nationwide have no published shape ({', '.join(missing[:10])})" if missing else None,
                       f"{no_detail} TFR detail files unavailable" if no_detail else None)
    # drop cached detail files for TFRs no longer listed
    cdir = ctx.out / "_cache" / "tfr"
    if cdir.exists() and not ctx.bbox:
        keep = {f"{n.replace('/', '_')}_{m}.xml" for n, m in mods.items()}
        for f in cdir.glob("*.xml"):
            if f.name not in keep:
                f.unlink(missing_ok=True)
    yield out


# ----------------------------------------------------------------------------- NOTAM API (keys required)

NOTAM_KINDS = [("91.141", "VIP / presidential movement"), ("91.137", "disaster/hazard area"),
               ("91.138", "disaster area in Hawaii"), ("91.143", "space operations")]


def notam_coord(s):
    """'3859N07718W' or '385900N0771800W' -> (lon, lat)."""
    m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})?(?:\.\d+)?([NS])(\d{3})(\d{2})(\d{2})?(?:\.\d+)?([EW])", (s or "").strip())
    if not m:
        return None
    lat = int(m[1]) + int(m[2]) / 60 + int(m[3] or 0) / 3600
    lon = int(m[5]) + int(m[6]) / 60 + int(m[7] or 0) / 3600
    return (-lon if m[8] == "W" else lon), (-lat if m[4] == "S" else lat)


def notam_geometry(geom, n):
    polys = []
    stack = [geom] if geom else []
    while stack:
        g = stack.pop()
        if g.get("type") == "GeometryCollection":
            stack += g.get("geometries") or []
        elif g.get("type") == "Polygon":
            polys.append(g["coordinates"])
        elif g.get("type") == "MultiPolygon":
            polys += g["coordinates"]
    if polys:
        return ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                else {"type": "MultiPolygon", "coordinates": polys}), "medium"
    c, rad = notam_coord(n.get("coordinates")), num(n.get("radius"))
    if c and rad:
        return circle(c[0], c[1], rad * METERS_PER_NM), "low"
    return None, None


def src_notams(ctx):
    ctx.source = NOTAM_API
    cid, secret = os.environ.get("FAA_NOTAM_CLIENT_ID"), os.environ.get("FAA_NOTAM_CLIENT_SECRET")
    if not (cid and secret):
        raise Skip("set FAA_NOTAM_CLIENT_ID and FAA_NOTAM_CLIENT_SECRET (free keys from api.faa.gov)")
    headers = {"client_id": cid, "client_secret": secret}
    out, page, last, no_geom = [], 1, None, 0
    while True:
        data = get_json(NOTAM_API, {"responseFormat": "geoJson", "classification": "FDC",
                                    "pageSize": 1000, "pageNum": page}, headers=headers)
        for item in data.get("items") or []:
            n = (((item.get("properties") or {}).get("coreNOTAMData") or {}).get("notam")) or {}
            text = n.get("text") or ""
            if "TEMPORARY FLIGHT RESTRICTION" not in text.upper():
                continue
            kind = next(((sec, label) for sec, label in NOTAM_KINDS if sec in text), None)
            if not kind:
                continue
            geom, conf = notam_geometry(item.get("geometry"), n)
            if not geom:
                no_geom += 1
                continue
            if not in_bbox(geom, ctx.bbox):
                continue
            last = max(filter(None, [last, n.get("lastUpdated")]), default=None)
            lo, hi = num(n.get("minimumFL")), num(n.get("maximumFL"))
            end = n.get("effectiveEnd")
            out.append(rule(geom, "notam_tfr", n.get("id") or n.get("number"), f"FDC {n.get('number')} {n.get('location') or ''}".strip(),
                            "prohibited", (lo * 100 if lo is not None else None, "FL" if lo is not None else None),
                            (hi * 100 if hi is not None else None, "FL" if hi is not None else None),
                            (iso(n.get("effectiveStart")), None if (end or "").upper().startswith("PERM") else iso(end)),
                            notes=join(f"TFR NOTAM: {kind[1]}. Floor/ceiling are the NOTAM Q-line flight levels (coarse); read text.",
                                       text[:1500]),
                            citation=f"14 CFR {kind[0]} ({kind[1]})", case=f"NOTAM:{kind[0]}:{'POLYGON' if conf == 'medium' else 'RADIUS'}", source_url=NOTAM_API,
                            attrs={k: v for k, v in n.items() if k != "text"}))
        total_pages = int(data.get("totalPages") or 1)
        if page >= total_pages or not data.get("items"):
            break
        page += 1
    ctx.last_edit = last
    ctx.message = f"{no_geom} matching TFR NOTAMs had no geometry" if no_geom else None
    yield out


# ----------------------------------------------------------------------------- DC FRZ (14 CFR 93.335)

def _dms(d, m, s_, hemi):
    v = d + m / 60 + s_ / 3600
    return -v if hemi in "SW" else v


DCA = (_dms(77, 2, 11, "W"), _dms(38, 51, 34, "N"))          # Washington VOR/DME (DCA), per 93.335


def _bearing(a, b):
    lon1, lat1, lon2, lat2 = map(math.radians, (*a, *b))
    y = math.sin(lon2 - lon1) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1)
    return math.degrees(math.atan2(y, x)) % 360


def _dest(c, brg, nm):
    R_, d, b = 6371008.8, nm * METERS_PER_NM / 6371008.8, math.radians(brg)
    lat1, lon1 = math.radians(c[1]), math.radians(c[0])
    lat2 = math.asin(math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(b))
    lon2 = lon1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(lat1), math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return (math.degrees(lon2), math.degrees(lat2))


def _arc(c, a, b, nm, step=2.0):
    """Clockwise arc around c from point a to point b (true bearings computed from the given points)."""
    b0, b1 = _bearing(c, a), _bearing(c, b)
    span = (b1 - b0) % 360
    n = max(2, int(span / step))
    return [_dest(c, b0 + span * i / n, nm) for i in range(n + 1)]


def src_dc_frz(ctx):
    from shapely.geometry import Polygon, Point
    p1 = (_dms(77, 18, 30, "W"), _dms(38, 59, 31, "N"))    # DCA 311 deg / 15 NM
    p2 = (_dms(77, 4, 32, "W"), _dms(39, 6, 28, "N"))      # DCA 002 deg / 15 NM
    p3 = (_dms(76, 50, 38, "W"), _dms(39, 2, 18, "N"))     # DCA 049 deg / 14 NM
    p4 = (_dms(76, 48, 32, "W"), _dms(38, 59, 1, "N"))     # DCA 064 deg / 13 NM
    p5 = (_dms(77, 18, 48, "W"), _dms(38, 50, 53, "N"))    # DCA 276 deg / 13 NM
    ring = _arc(DCA, p1, p2, 15) + [p3] + _arc(DCA, p4, p5, 13) + [p1]
    frz = Polygon(ring)
    # No Freeway Airport (W00) cutout (final review 2026-10-06): 93.335 excludes it for manned aircraft,
    # but the drone ban (99.7) is not confirmed to exclude it.
    note = "Exact boundary from 14 CFR 93.335 (DCA 15 NM / 13 NM arcs and connecting lines)"
    ctx.source, ctx.last_edit = "https://www.ecfr.gov/current/title-14/section-93.335", None
    g = frz.__geo_interface__
    if ctx.bbox and not in_bbox(g, ctx.bbox):
        yield []
        return
    yield [rule({"type": g["type"], "coordinates": g["coordinates"]}, "dc_frz", "DC_FRZ",
                "Washington, DC Flight Restricted Zone (FRZ)", "prohibited", (0.0, "AGL"), (18000.0, "MSL"),
                notes=join("Drone flight in the DC FRZ is not allowed without FAA/TSA authorization",
                           "The surrounding DC SFRA ring (15-30 NM) is open to recreational and Part 107 flyers who follow its rules",
                           note, "Ceiling: up to but not including FL180"),
                citation="14 CFR 93.339; 14 CFR 93.335", case="DC_FRZ", source_url=ctx.source)]


# ----------------------------------------------------------------------------- layer registry

LAYERS = {   # name: (group, source)
    "seams": ("fast", arcgis_source(SEAMS_URL, r_seams)),
    "tfr_active": ("fast", src_tfr),
    "notam_tfr": ("fast", src_notams),
    "nsufr": ("daily", arcgis_source("DoD_Mar_13", r_nsufr)),
    "nsufr_part_time": ("daily", arcgis_source("Part_Time_National_Security_UAS_Flight_Restrictions", r_nsufr_pt)),
    "nda_tfr": ("daily", arcgis_source("National_Defense_Airspace_TFR_Areas", r_nda)),
    "laanc_grid": ("weekly", arcgis_source("FAA_UAS_FacilityMap_Data", r_laanc,
                                           "OBJECTID,GLOBALID,CEILING,UNIT,MAP_EFF,LAST_EDIT,APT1_FAAID,APT1_NAME,"
                                           "APT1_LAANC,AIRSPACE_1,REGION")),
    "class_airspace": ("weekly", arcgis_source("Class_Airspace", r_class, page_size=250)),
    "special_use_airspace": ("weekly", arcgis_source("Special_Use_Airspace", r_sua)),
    "prohibited_areas": ("weekly", arcgis_source("Prohibited_Areas", r_prohibited)),
    "dc_frz": ("weekly", src_dc_frz),
    "stadiums_3nm": ("weekly", arcgis_source("Stadiums", r_stadium)),
    "fria": ("weekly", arcgis_source("FAA_Recognized_Identification_Areas", r_fria)),
    "recreational_fixed_sites": ("weekly", arcgis_source("Recreational_Flyer_Fixed_Sites", r_fixed_site)),
}
GROUPS = ["fast", "daily", "weekly"]


# ----------------------------------------------------------------------------- output

class FileLock:
    """Cross-process lock so overlapping scheduled runs don't write the GeoPackage/manifest at once."""

    def __init__(self, path, wait_s=1800, stale_s=7200):
        self.path, self.wait_s, self.stale_s = Path(path), wait_s, stale_s

    def __enter__(self):
        t0 = time.time()
        while True:
            try:
                os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_s:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                if time.time() - t0 > self.wait_s:
                    raise TimeoutError(f"could not get lock {self.path}")
                time.sleep(2)

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def to_gdf(feats):
    feats = [f for f in feats if (f.get("geometry") or {}).get("type") in ("Polygon", "MultiPolygon")]
    if feats:
        gdf = gpd.GeoDataFrame.from_features(feats, crs=4326, columns=COLUMNS + ["geometry"])
    else:
        gdf = gpd.GeoDataFrame({c: [] for c in COLUMNS}, geometry=gpd.GeoSeries([], crs=4326), crs=4326)
    for c in COLUMNS:          # fixed dtypes so pages append cleanly
        if c in NUMERIC:
            gdf[c] = pd.to_numeric(gdf[c], errors="coerce").astype("float64")
        else:
            gdf[c] = pd.Series([None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v)
                                for v in gdf[c]], index=gdf.index, dtype=object)
    return gdf[COLUMNS + ["geometry"]]


def gpkg_write(gdf, path, layer, append):
    gdf.to_file(path, layer=layer, driver="GPKG", mode="a" if append else "w",
                promote_to_multi=True, geometry_type="MultiPolygon")


def staging_dir(out):
    return out / "_staging" / str(os.getpid())     # per process, so overlapping runs don't collide


class LayerWriter:
    """Writes pages to staging files; commit() swaps them in so a failed run never leaves a half layer."""

    def __init__(self, out, name, want_geojson):
        self.out, self.name, self.count = out, name, 0
        self.stage = staging_dir(out)
        self.stage.mkdir(parents=True, exist_ok=True)
        self.gpkg = self.stage / f"{name}.gpkg" if HAVE_GPD else None
        self.gj_path = self.stage / f"{name}.geojson" if want_geojson else None
        self.gj = None
        if self.gpkg and self.gpkg.exists():
            self.gpkg.unlink()
        if self.gj_path:
            self.gj = open(self.gj_path, "w", encoding="utf-8")
            self.gj.write('{"type":"FeatureCollection","features":[\n')

    def write(self, feats, stamp):
        for f in feats:
            f["properties"].update(stamp)
        if self.gj:
            for f in feats:
                self.gj.write(("," if self.count else "") + json.dumps(f) + "\n")
                self.count += 1
        else:
            self.count += len(feats)
        if self.gpkg and feats:
            gpkg_write(to_gdf(feats), self.gpkg, self.name, append=self.gpkg.exists())

    def commit(self, main_gpkg, lock):
        if self.gj:
            self.gj.write("]}\n")
            self.gj.close()
            (self.out / "geojson").mkdir(exist_ok=True)
            os.replace(self.gj_path, self.out / "geojson" / f"{self.name}.geojson")
        if not self.gpkg:
            return
        with lock:
            if not self.gpkg.exists():          # zero features: still replace the layer so stale rules vanish
                gpkg_write(to_gdf([]), main_gpkg, self.name, append=False)
                return
            n = pyogrio.read_info(self.gpkg, layer=self.name)["features"]
            for i in range(0, n, 50000):
                part = gpd.read_file(self.gpkg, layer=self.name, rows=slice(i, i + 50000))
                gpkg_write(part, main_gpkg, self.name, append=i > 0)
        self.gpkg.unlink(missing_ok=True)

    def abort(self):
        if self.gj:
            self.gj.close()
            self.gj_path.unlink(missing_ok=True)
        if self.gpkg:
            self.gpkg.unlink(missing_ok=True)


def update_manifest(path, rows, lock):
    with lock:
        existing = {}
        if path.exists():
            with open(path, newline="", encoding="utf-8") as f:
                existing = {r["layer"]: r for r in csv.DictReader(f)}
        existing.update({r["layer"]: r for r in rows})
        order = list(LAYERS)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_COLS, extrasaction="ignore")
            w.writeheader()
            for name in sorted(existing, key=lambda n: order.index(n) if n in order else 99):
                w.writerow(existing[name])
        os.replace(tmp, path)


class Ctx:
    def __init__(self, out, bbox):
        self.out, self.bbox = out, bbox
        self.source = self.last_edit = self.message = None


def run_layer(name, group, source, out, bbox, want_geojson, main_gpkg, lock):
    ctx, t0 = Ctx(out, bbox), time.time()
    fetched = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    row = {"layer": name, "group": group, "fetched_utc": fetched,
           "bbox": ",".join(map(str, bbox)) if bbox else "full"}
    print(f"{name:<26}", end="", flush=True)
    writer = None
    try:
        for feats in source(ctx):
            if writer is None:
                writer = LayerWriter(out, name, want_geojson)
            writer.write(feats, {"source_last_edit": ctx.last_edit, "fetched_utc": fetched})
            if writer.count >= 10000:
                print(f"\r{name:<26}{writer.count:>8} ...", end="", flush=True)
        writer = writer or LayerWriter(out, name, want_geojson)
        writer.commit(main_gpkg, lock)
        row.update(status="ok", count=writer.count)
    except Skip as e:
        row.update(status="skipped", count="", message=str(e))
    except Exception as e:
        if writer:
            writer.abort()
        row.update(status="error", count="", message=f"{type(e).__name__}: {e}")
    row.update(server_last_edit=ctx.last_edit or "", source=ctx.source or "",
               seconds=round(time.time() - t0, 1))
    if row["status"] == "ok" and ctx.message:
        row["message"] = ctx.message
    print(f"\r{name:<26}{row['status']:<8}{row['count']!s:>8}  last edit {row['server_last_edit'] or '-'}"
          + (f"  ({row.get('message')})" if row.get("message") else ""))
    return row


def renormalize(out, names, lock):
    """Re-apply the labeling rules (and rule_decisions.json) to an existing GeoPackage, using the
    FAA fields stored in source_attrs. No download. TFR/NOTAM layers need --group fast instead."""
    gpkg = out / "uas_rules.gpkg"
    have = {n for n, _ in pyogrio.list_layers(gpkg)} if gpkg.exists() else set()
    for name in names:
        to_rule = getattr(LAYERS[name][1], "to_rule", None)
        if not to_rule or name not in have:
            print(f"{name:<26}skipped" + ("" if to_rule else " (re-download with --group fast)"))
            continue
        g = gpd.read_file(gpkg, layer=name)
        writer, batch = LayerWriter(out, name, False), []
        for row in g.itertuples(index=False):
            geom = row.geometry.centroid if name == "stadiums_3nm" else row.geometry
            f = to_rule(geom.__geo_interface__, json.loads(row.source_attrs or "{}"), row.source_url)
            if f:
                f["properties"].update(source_last_edit=row.source_last_edit, fetched_utc=row.fetched_utc)
                batch.append(f)
            if len(batch) >= 5000:
                writer.write(batch, {}); batch = []
        writer.write(batch, {})
        writer.commit(gpkg, lock)
        print(f"{name:<26}relabeled {writer.count:>8}")
    shutil.rmtree(staging_dir(out), ignore_errors=True)


def parse_bbox(s):
    try:
        b = [float(x) for x in s.split(",")]
    except ValueError:
        b = []
    if len(b) != 4 or not (b[0] < b[2] and b[1] < b[3]):
        raise argparse.ArgumentTypeError("bbox must be minLon,minLat,maxLon,maxLat (e.g. -77.6,38.5,-76.9,39.1)")
    return b


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # "--bbox -77.6,..." would look like a flag to argparse; glue it into "--bbox=-77.6,..."
    for i, a in enumerate(argv[:-1]):
        if a == "--bbox":
            argv[i:i + 2] = [f"--bbox={argv[i + 1]}"]
            break
    ap = argparse.ArgumentParser(description="Build normalized FAA drone-rule layers into out/uas_rules.gpkg")
    ap.add_argument("--bbox", type=parse_bbox, help="minLon,minLat,maxLon,maxLat (default: whole US)")
    ap.add_argument("--group", choices=GROUPS + ["all"], default="all", help="refresh only one group of layers")
    ap.add_argument("--layers", help="comma-separated layer names (overrides --group)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "out"), help="output folder")
    ap.add_argument("--geojson", action="store_true", help="also write GeoJSON when geopandas is installed")
    ap.add_argument("--renormalize", action="store_true",
                    help="re-label the existing GeoPackage with current rules/decisions (no download)")
    args = ap.parse_args(argv)

    if args.layers:
        names = [n.strip() for n in args.layers.split(",") if n.strip()]
        bad = [n for n in names if n not in LAYERS]
        if bad:
            ap.error(f"unknown layer(s): {', '.join(bad)}. Choose from: {', '.join(LAYERS)}")
    else:
        names = [n for n, (g, _) in LAYERS.items() if args.group in ("all", g)]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    main_gpkg, lock = out / "uas_rules.gpkg", FileLock(out / ".uas_rules.lock")
    want_geojson = args.geojson or not HAVE_GPD
    if not HAVE_GPD:
        print("geopandas not installed: writing GeoJSON only (out/geojson/), no GeoPackage.\n")
    print(f"Area: {'bbox ' + ','.join(map(str, args.bbox)) if args.bbox else 'whole US'}   Layers: {len(names)}\n")

    if args.renormalize:
        if not HAVE_GPD:
            sys.exit("--renormalize needs geopandas")
        renormalize(out, names, lock)
        return 0

    rows = []
    for name in names:
        group, source = LAYERS[name]
        rows.append(run_layer(name, group, source, out, args.bbox, want_geojson, main_gpkg, lock))
        update_manifest(out / "manifest.csv", [rows[-1]], lock)
    shutil.rmtree(staging_dir(out), ignore_errors=True)
    try:
        staging_dir(out).parent.rmdir()          # only succeeds if no other run is using it
    except OSError:
        pass

    errors = [r["layer"] for r in rows if r["status"] == "error"]
    print(f"\nDone. Output: {out}")
    if HAVE_GPD:
        print(f"GeoPackage: {main_gpkg}")
    print(f"Manifest:   {out / 'manifest.csv'}")
    if errors:
        print(f"Layers with errors: {', '.join(errors)} (see manifest.csv 'message')")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
