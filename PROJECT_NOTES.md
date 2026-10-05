# UAS Airspace Map: project notes (handoff summary)

Condensed record of the build session (Oct 1–5, 2026) so work can continue in a new chat.
Repo: https://github.com/nicovacca/uas-airspace (private). Local: `C:\Users\Nico\R\GIT-UAS MAP`.

## Goal
Pull every public FAA drone-airspace dataset, normalize it into one GeoPackage, and show it on an
AirHub-style interactive map: click anywhere → every rule at that spot, in plain English, with
altitudes, active times, citation and the raw FAA fields. Current focus area: New York State.

## Files
| File | Role |
|---|---|
| `build_uas_layers.py` | Downloads all layers → `out/uas_rules.gpkg` + `out/manifest.csv`. Flags: `--bbox minLon,minLat,maxLon,maxLat`, `--group fast/daily/weekly`, `--layers a,b`, `--geojson`, `--out` |
| `make_uas_map.py` | GeoPackage → single shareable `out/uas_map.html` (Leaflet, light theme). `--title "New York"` |
| `uas_app.py` + `uas_app_map.html` | Streamlit app, dark AirHub-style Leaflet map (layers drawer with toggles, status pill, briefing panel, weather via Open-Meteo, FAA airports). Sidebar: area picker, "Download all", "Refresh TFRs" |
| `UAS_Data_Sources.docx` | Every dataset used/not used, FAA field names + meanings |
| `.streamlit/config.toml`, `requirements.txt`, `.gitignore`, `README.md` | Setup. `out/` and `.venv/` are not committed |
| `faa_drone_zones.py` | First prototype; superseded |

Run (PowerShell, in repo folder):
```
python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python build_uas_layers.py --bbox -79.8,40.4,-71.8,45.1   # NY, ~1 min; full US ~25 min
.venv\Scripts\streamlit run uas_app.py                                  # or: make_uas_map.py --title "New York"
```

## Data sources (all public, no keys)
FAA ArcGIS REST `services6.arcgis.com/ssFJjBXIUyZDrSYZ/arcgis/rest/services/<svc>/FeatureServer/0/query`:
- weekly: `FAA_UAS_FacilityMap_Data` (LAANC grid, 380,860 cells), `Class_Airspace`, `Special_Use_Airspace`, `Prohibited_Areas`, `Stadiums` (→ 3 NM circles), `FAA_Recognized_Identification_Areas`, `Recreational_Flyer_Fixed_Sites`
- daily: `DoD_Mar_13` (= National Security UAS Flight Restrictions), `Part_Time_National_Security_UAS_Flight_Restrictions`, `National_Defense_Airspace_TFR_Areas`
- app only: `US_Airport`
- fast: SEAMS `services1.arcgis.com/n4Ot9Qz0t5espY4s/.../SEAMS_Production_View/FeatureServer/0` (stadium event TFR times); tfr.faa.gov `/tfrapi/getTfrList`, GeoServer WFS `TFR:V_TFR_LOC` (shapes), `/download/detail_N_NNNN.xml` (times/altitudes/text)
- NOT pulled yet: FAA NOTAM API (`external-api.faa.gov/notamapi/v1/notams`) needs keys from api.faa.gov in `FAA_NOTAM_CLIENT_ID` / `FAA_NOTAM_CLIENT_SECRET`; code exists, untested
- Unused but relevant FAA services: `Airspace_Schedule` (part-time Class D hours, XML timesheets), `UAS_NSR_Pending`, `Digital_Obstacle_File` (656k obstacles), `MTRSegment`, `Operating_Categories_Map` (POP_DENSITY, undocumented)

## Normalized schema (every layer)
`layer, source_id, name, effect (prohibited|restricted|authorization_required|caution|allowed), floor_ft, floor_ref, ceiling_ft, ceiling_ref (AGL|MSL|FL|UNL|NOTAM), window_start, window_end (UTC; empty = permanent), notes, citation, confidence (high|medium|low), source_url, source_last_edit, fetched_utc, source_attrs (raw FAA JSON)`

## Gotchas learned (keep these)
- tfr.faa.gov XML `dateEffective/dateExpire` are **UTC even when `codeTimeZone` says EDT** (checked against NOTAM text).
- Multi-area TFRs: WFS shapes can't be matched 1:1 to XML areas → overall min floor / max ceiling + per-area list in notes.
- ADDS altitude sentinels: `UPPER_VAL -9998` + `UPPER_DESC AA` = up to but not incl. 18,000 ft MSL (4,203 records, mostly Class E); `UPPER_CODE BYNOTAM` = set by NOTAM; `UNLTD` = unlimited. Fixed Oct 5.
- FAA ArcGIS quota: 6,000 request units/min → HTTP-200 body with error code 429; script waits 61 s and retries. Class_Airspace pages time out (504) at 2,000 → page size 250 + auto-halving.
- `--bbox -77.6,...` breaks argparse (negative number looks like a flag) → script rewrites to `--bbox=...`.
- CARTO basemaps now need an API key → use Esri tiles. `st.components.v1.html` deprecated → `st.iframe`.
- FAA records include POC names/emails/phones → never commit `out/`, omit POC in docs.
- Windows: project venv at `.venv`; Python 3.12; Word available for docx→PDF via COM.

## Open issues / next steps
1. Map's circular-arrows button only resets the view (not a refresh). Make it trigger `--group fast`; optional 5-min auto-refresh.
2. Get NOTAM API keys and test `notam_tfr`.
3. Join `Airspace_Schedule` to Class D for real tower hours.
4. Verify citations (best-effort: e.g. stadium = 14 CFR 99.7 / Pub. L. 108-7 §352; fixed sites = 49 U.S.C. 44809(a)(5)).
5. Optional: clip to NY state border (`--state NY`), MapLibre GL instead of Leaflet, Windows Task Scheduler jobs (fast 5 min / daily / weekly).
6. Add state and local regulations (plan below).

## Plan: state and local regulations
Key distinction: the FAA controls the **airspace**. States and localities mostly regulate the **ground**: where you may take off and land (parks, city property), privacy and trespass, and flights over critical infrastructure. So these become separate "ground rule" layers, not airspace.

No single national dataset exists. Build it as **curated rules + public boundaries**:
1. `local_rules.csv` (versioned in git, researched by hand): `rule_id, jurisdiction, level (state|county|city|park_agency), rule_type (takeoff_landing|park|critical_infrastructure|privacy|permit), effect, summary, citation, url, applies_to (all|recreational|part107), last_verified`.
2. Boundary sources to join to: Census TIGER (states, counties, places), NPS park boundaries (national parks: drone takeoff/landing banned under 36 CFR 1.5 closures), USFS designated wilderness, NYS GIS Clearinghouse (state parks), NYC Open Data (parks), HIFLD (critical infrastructure, for states with CI drone laws).
3. `build_local_layers.py`: joins rules to boundaries → new layers in the same GeoPackage with the same schema (+ `jurisdiction`, `rule_type`, `last_verified`), `effect` mapped to the existing scale.
4. Briefing shows two sections: "Airspace (FAA)" and "Ground and local rules", each with a citation link and its last-verified date.
5. Research sources: NCSL state UAS legislation tracker, state DOT aviation offices, city codes (e.g. NYC Admin. Code §10-126(c) restricts takeoff/landing in the city; NYS Parks permit rules). Verify each before adding, and record `last_verified`.
