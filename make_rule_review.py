r"""
make_rule_review.py - build review/rule_review.html: one card per labeling rule case.

Each card shows how many features the case covers, real examples with their FAA fields,
the label the code assigns today, and the governing regulation quoted from the eCFR.
Pick a decision for each case and click "Export decisions" to save rule_decisions.json
into this folder. build_uas_layers.py then applies it on every build
(apply to existing data without downloading: build_uas_layers.py --out out_full --renormalize).

Run: .venv\Scripts\python make_rule_review.py      (reads out_full/uas_rules.gpkg)
"""

import argparse
import json
import re
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path

import geopandas as gpd
import pyogrio
import requests

HERE = Path(__file__).resolve().parent
EFFECTS = ["prohibited", "restricted", "authorization_required", "caution", "allowed"]
ECFR = "https://www.ecfr.gov/api/versioner/v1"
STADIUM_PAGE = "https://www.faa.gov/uas/getting_started/where_can_i_fly/airspace_restrictions/sports_stadiums"
USC_44809 = "https://www.law.cornell.edu/uscode/text/49/44809"

# What the code does for each case (or case prefix) and which rules govern it.
# refs: eCFR sections to quote (title 14) and/or (label, url) links.
CASES = {
    "LAANC:CEILING>0": ("LAANC grid square with a ceiling above 0 ft: controlled airspace near an airport; LAANC can pre-approve up to CEILING ft AGL.",
                        ["107.41"], [("49 U.S.C. 44809(a)(5) (recreational flyers)", USC_44809)]),
    "LAANC:CEILING=0": ("LAANC grid square with CEILING 0: controlled airspace where LAANC cannot auto-approve; a FAADroneZone request is needed.",
                        ["107.41"], [("49 U.S.C. 44809(a)(5) (recreational flyers)", USC_44809)]),
    "CLASS:CLASS_B": ("Class B airspace (surface-based or shelf).", ["107.41"], []),
    "CLASS:CLASS_C": ("Class C airspace.", ["107.41"], []),
    "CLASS:CLASS_D": ("Class D airspace (towered airport; may be part-time).", ["107.41"], []),
    "CLASS:CLASS_E2": ("Class E surface area designated for an airport.", ["107.41"], []),
    "CLASS:CLASS_E3": ("Class E extension to a Class C/D surface area (starts at the surface).", ["107.41"], []),
    "CLASS:CLASS_E4": ("Class E extension to a Class E surface area (starts at the surface).", ["107.41"], []),
    "CLASS:CLASS_E5": ("Class E starting at 700 or 1,200 ft AGL (or higher).", ["107.41", "107.51"], []),
    "CLASS:": ("Other class-airspace feature (Class A, E6, Mode C veil, etc.).", ["107.41", "107.51"], []),
    "SUA:P": ("Special use airspace type P (prohibited area).", ["73.83", "91.133"], []),
    "PROHIBITED:P": ("Prohibited area from the Prohibited_Areas layer (e.g. P-56).", ["73.83", "107.45", "99.7"], []),
    "SUA:R": ("Restricted area: hazardous activity; permission needed while active.", ["73.13", "107.45"], []),
    "SUA:": ("MOA, warning, alert or danger area: no CFR flight prohibition for drones; advisory.", [],
             [("FAA Order JO 7400.10 (Special Use Airspace)", "https://www.faa.gov/air_traffic/publications")]),
    "NSUFR:FULL_TIME": ("National Security UAS Flight Restriction, 24/7.", ["99.7", "107.47"], []),
    "NSUFR:PART_TIME:WINDOW": ("Part-time national security restriction with a published active window.", ["99.7", "107.47"], []),
    "NSUFR:PART_TIME:NO_WINDOW": ("Part-time national security restriction with no active window published (status unknown).", ["99.7", "107.47"], []),
    "NDA:BY_NOTAM": ("National Defense Airspace area: becomes a TFR only when activated by NOTAM; no altitudes or times in the data.", ["99.7", "107.47"], []),
    "STADIUM:RING": ("3 NM ring drawn around an FAA stadium point; restriction applies only during qualifying events.", ["99.7", "107.47"],
                     [("FAA: Stadiums and sporting events", STADIUM_PAGE)]),
    "SEAMS:EVENT": ("Stadium TFR for one scheduled event (SEAMS), with exact start/end.", ["99.7", "107.47"],
                    [("FAA: Stadiums and sporting events", STADIUM_PAGE)]),
    "TFR:FLOOR_NEAR_SURFACE": ("TFR whose lowest floor is at or below 400 ft AGL.", ["107.47"], []),
    "TFR:FLOOR_ABOVE_400_AGL": ("TFR whose lowest floor is above 400 ft AGL (currently labeled Advisory).", ["107.47", "107.51"], []),
    "TFR:FLOOR_MSL": ("TFR whose floor is given in MSL or flight level: whether it reaches the ground depends on terrain.", ["107.47", "107.51"], []),
    "TFR:FLOOR_MSL:NEAR_TERRAIN": ("TFR with an MSL floor that comes within 400 ft of the highest terrain under it (Open-Meteo / Copernicus 90 m DEM).", ["107.47", "107.51"], []),
    "TFR:FLOOR_MSL:TERRAIN_UNCHECKED": ("TFR with an MSL floor where the terrain lookup failed.", ["107.47", "107.51"], []),
    "TFR:NO_DETAILS": ("TFR shape with no detail file (no altitudes or times).", ["107.47"], []),
    "CLASS:CLASS_B:SHELF": ("Class B shelf: part of Class B whose floor is above the surface.", ["107.41", "107.51"], []),
    "CLASS:CLASS_C:SHELF": ("Class C shelf: part of Class C whose floor is above the surface.", ["107.41", "107.51"], []),
    "SUA:R:ELEVATED_FLOOR": ("Restricted area whose floor is above the surface.", ["73.13", "107.45"], []),
    "DC_FRZ": ("Washington, DC Flight Restricted Zone, exact boundary from 14 CFR 93.335.", ["93.335", "93.339"], []),
    "TFR:": ("TFR with several areas: shapes cannot be matched to areas, so the overall lowest floor and highest ceiling are used.", ["107.47"], []),
    "FRIA": ("FAA-Recognized Identification Area: drones without Remote ID may fly here.", ["89.115"], []),
    "FIXED_SITE": ("Recreational flyer fixed site with an FAA agreement.", [], [("49 U.S.C. 44809(a)(5)", USC_44809)]),
    "NOTAM:": ("TFR taken from the FAA NOTAM API.", ["107.47"], []),
}
KEY_FIELDS = {
    "laanc_grid": ["APT1_NAME", "APT1_FAAID", "CEILING", "AIRSPACE_1", "grid_cells"],
    "class_airspace": ["NAME", "CLASS", "LOCAL_TYPE", "LOWER_VAL", "LOWER_CODE", "UPPER_VAL", "UPPER_DESC", "WKHR_CODE", "WKHR_RMK"],
    "special_use_airspace": ["NAME", "TYPE_CODE", "LOWER_VAL", "LOWER_CODE", "UPPER_VAL", "UPPER_CODE", "TIMESOFUSE", "CONT_AGENT"],
    "prohibited_areas": ["NAME", "TYPE_CODE", "UPPER_VAL", "UPPER_CODE", "TIMESOFUSE"],
    "nsufr": ["Facility", "Base", "Branch", "Floor", "Ceiling", "Reason", "State"],
    "nsufr_part_time": ["Facility", "ALERTYPE", "ACTIVETIME", "ENDTIME", "Floor", "Ceiling"],
    "nda_tfr": ["NAME", "WKHR_RMK", "STATE"],
    "stadiums_3nm": ["NAME", "CITY", "STATE", "STATUS_CODE"],
    "seams": ["EVENT_NAME", "VENUE", "LEAGUE_NAME", "STATUS"],
    "tfr_active": ["TITLE", "LEGAL", "NOTAM_KEY"],
    "fria": ["title", "orgName", "city", "state", "endDate"],
    "recreational_fixed_sites": ["SITE_NAME", "CITY", "STATE", "CEILING"],
}


def case_info(case):
    if case in CASES:
        return CASES[case]
    for k, v in CASES.items():            # prefix entries end with ":"
        if k.endswith(":") and case.startswith(k):
            return v
    return ("(no description)", [], [])


def ecfr_sections(sections):
    titles = requests.get(f"{ECFR}/titles.json", timeout=60).json()["titles"]
    date = next(t for t in titles if t["number"] == 14)["latest_issue_date"]
    out = {}
    for sec in sorted(sections):
        part = sec.split(".")[0]
        try:
            r = requests.get(f"{ECFR}/full/{date}/title-14.xml", params={"part": part, "section": sec}, timeout=60)
            root = ET.fromstring(r.content)
            text = "\n".join(re.sub(r"\s+", " ", " ".join(p.itertext())).strip() for p in root.iter("P"))
            out[sec] = {"head": re.sub(r"^\W+", "", root.findtext(".//HEAD") or sec), "text": text,
                        "url": f"https://www.ecfr.gov/current/title-14/section-{sec}"}
        except Exception as e:
            out[sec] = {"head": f"§ {sec}", "text": f"(could not load: {e})", "url": f"https://www.ecfr.gov/current/title-14/section-{sec}"}
    return out, date


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gpkg", default=str(HERE / "out_full" / "uas_rules.gpkg"))
    ap.add_argument("--out", default=str(HERE / "review" / "rule_review.html"))
    args = ap.parse_args()

    rows = []
    for layer, _ in pyogrio.list_layers(args.gpkg):
        g = gpd.read_file(args.gpkg, layer=layer, ignore_geometry=True)
        if g.empty or "rule_case" not in g:
            continue
        for case, grp in g.groupby("rule_case"):
            ex = grp.drop_duplicates("name").sample(min(3, grp["name"].nunique()), random_state=1) if len(grp) else grp
            examples = []
            for r in ex.itertuples(index=False):
                a = json.loads(r.source_attrs or "{}")
                examples.append({"name": r.name, "effect": r.effect,
                                 "alt": f"{r.floor_ft if r.floor_ft == r.floor_ft else '—'} {r.floor_ref or ''} → {r.ceiling_ft if r.ceiling_ft == r.ceiling_ft else '—'} {r.ceiling_ref or ''}",
                                 "fields": {k: a.get(k) for k in KEY_FIELDS.get(layer, []) if a.get(k) not in (None, "")}})
            rows.append({"case": case, "layer": layer, "count": len(grp), "effect": grp["effect"].mode()[0],
                         "citation": grp["citation"].mode()[0] if grp["citation"].notna().any() else "", "examples": examples})
    rows.sort(key=lambda r: (r["layer"], r["case"]))
    secs = {s for r in rows for s in case_info(r["case"])[1]}
    print(f"{len(rows)} rule cases; fetching {len(secs)} eCFR sections...")
    ecfr, ecfr_date = ecfr_sections(secs)
    full = {}
    if (HERE / "rule_decisions.json").exists():
        full = json.loads((HERE / "rule_decisions.json").read_text(encoding="utf-8"))
    existing = full.get("decisions", full)
    seen = {r["case"] for r in rows}
    for k, v in existing.items():            # decided cases with no features left (e.g. foreign airspace now dropped)
        if isinstance(v, dict) and k not in seen:
            rows.append({"case": k, "layer": v.get("layer", ""), "count": 0, "effect": v.get("decision", ""),
                         "citation": v.get("citation", ""), "examples": []})
    missing = {sec for r in rows for sec in case_info(r["case"])[1]} - set(ecfr)
    if missing:
        ecfr.update(ecfr_sections(missing)[0])

    cards = []
    for i, r in enumerate(rows):
        desc, secrefs, links = case_info(r["case"])
        dec = existing.get(r["case"], {})
        cur = dec.get("decision") or dec.get("effect") or r["effect"]
        flags = "".join(f'<li class="flag">{escape(f)}</li>' for f in dec.get("audit_flags", []))
        resol = "".join(f'<li class="{"open" if x.startswith("Open") else "res"}">{escape(x)}</li>' for x in dec.get("audit_resolution", []))
        audit = (f'<h4>Audit</h4><ul class="audit">{flags}{resol}</ul>' if flags or resol else "")
        badges = "".join([f'<span class="badge new">New case: needs review</span>' if dec.get("new_case") else "",
                          f'<span class="badge gone">0 features now (removed)</span>' if r["count"] == 0 else "",
                          f'<span class="badge chg">changed from {escape(dec["changed_from_default"])}</span>' if dec.get("changed_from_default") else ""])
        regs = "".join(f'<details class="reg"><summary><a href="{ecfr[s]["url"]}" target="_blank">14 CFR {escape(ecfr[s]["head"])}</a></summary>'
                       f'<p>{escape(ecfr[s]["text"][:2500])}{"…" if len(ecfr[s]["text"]) > 2500 else ""}</p></details>' for s in secrefs)
        regs += "".join(f'<div class="reg"><a href="{u}" target="_blank">{escape(t)}</a> (link only)</div>' for t, u in links)
        exs = "".join(f'<div class="ex"><b>{escape(str(e["name"]))}</b> <span class="muted">· {escape(e["alt"])}</span><br>'
                      + " · ".join(f'<code>{escape(k)}</code> {escape(str(v))}' for k, v in e["fields"].items()) + "</div>" for e in r["examples"])
        opts = "".join(f'<option value="{e}" {"selected" if e == cur else ""}>{e}{" (current default)" if e == r["effect"] else ""}</option>' for e in EFFECTS)
        cards.append(f'''<section class="card" data-case="{escape(r["case"])}" data-default="{r["effect"]}">
  <div class="top"><span class="num">{i + 1}</span><div><div class="case">{escape(r["case"])}</div>
    <div class="muted">layer <code>{r["layer"]}</code> · {r["count"]:,} features nationwide · label on the map now <b class="eff {r["effect"]}">{r["effect"]}</b> {badges}</div></div></div>
  <p class="desc">{escape(desc)}</p>
  <h4>Examples (real FAA records)</h4>{exs}
  <h4>Governing rule</h4>{regs or '<div class="muted">No regulation reference.</div>'}
  <div class="muted small">Citation shown on the map: {escape(r["citation"] or "—")}</div>
  {audit}
  <div class="decide"><label>Decision <select class="eff-sel">{opts}</select></label>
    <label class="grow">Note shown on the map (optional) <input class="note" value="{escape(dec.get("note", ""))}" placeholder="e.g. Advisory: floor is above the 400 ft limit"></label>
    <label class="chk"><input type="checkbox" class="ok" {"checked" if dec.get("reviewed") else ""}> Reviewed</label></div>
</section>''')

    html = TEMPLATE.replace("__ORIG__", json.dumps(full).replace("</", "<\\/")).replace("__CARDS__", "\n".join(cards)).replace("__N__", str(len(rows))).replace("__ECFR__", ecfr_date)
    out = Path(args.out); out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"Wrote {out}")


TEMPLATE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Rule case review</title>
<style>
 body { margin:0; background:#1d1d20; color:#eceef3; font:14px/1.5 Lato,system-ui,sans-serif; }
 header { position:sticky; top:0; z-index:5; background:#232428; border-bottom:1px solid #36373d; padding:12px 24px; display:flex; gap:16px; align-items:center; }
 header h1 { font-size:18px; margin:0; } header .sp { flex:1; }
 button { background:#2ea8ff; color:#04121f; border:0; border-radius:6px; padding:8px 14px; font-weight:700; cursor:pointer; }
 button.sec { background:#3a3c43; color:#eceef3; }
 main { max-width:1000px; margin:0 auto; padding:18px 24px 80px; }
 .intro { color:#c4c7cf; } .muted { color:#9a9ca6; } .small { font-size:12px; margin-top:6px; }
 .card { background:#26272b; border:1px solid #34353a; border-radius:10px; padding:16px 18px; margin:14px 0; }
 .card.done { border-color:#1f9d55; }
 .top { display:flex; gap:12px; align-items:flex-start; } .num { background:#3a3c43; border-radius:50%; width:28px; height:28px; display:grid; place-items:center; font-weight:700; flex:none; }
 .case { font-size:16px; font-weight:800; font-family:Consolas,monospace; }
 .desc { margin:10px 0 4px; } h4 { margin:12px 0 6px; font-size:12px; text-transform:uppercase; letter-spacing:.6px; color:#9a9ca6; }
 .ex { background:#1f2024; border-radius:6px; padding:7px 10px; margin-bottom:6px; font-size:13px; }
 code { background:#33353b; padding:0 4px; border-radius:3px; font-size:12px; color:#ffd8a8; }
 .reg { margin:4px 0; } .reg summary { cursor:pointer; } .reg p { white-space:pre-wrap; background:#1f2024; padding:10px; border-radius:6px; color:#d5d8df; font-size:13px; }
 a { color:#7cc6ff; }
 .eff { padding:1px 6px; border-radius:4px; } .eff.prohibited { background:#5c1a26; } .eff.restricted { background:#5c3418; }
 .eff.authorization_required { background:#163a5c; } .eff.caution { background:#3a2c5c; } .eff.allowed { background:#1c4a2c; }
 .audit { margin:4px 0 0; padding-left:18px; font-size:13px; } .audit li { margin:3px 0; }
 .audit .flag { color:#ffd166; } .audit .flag::marker { content:"⚑ "; } .audit .res { color:#8fd18b; } .audit .open { color:#ff9f43; }
 .badge { font-size:11px; font-weight:700; padding:1px 7px; border-radius:10px; margin-left:6px; }
 .badge.new { background:#5c4a12; color:#ffd166; } .badge.gone { background:#3a3b40; color:#a3a5ad; } .badge.chg { background:#163a5c; color:#7cc6ff; }
 .decide { display:flex; gap:14px; align-items:end; margin-top:14px; padding-top:12px; border-top:1px solid #36373d; flex-wrap:wrap; }
 .decide label { display:flex; flex-direction:column; gap:4px; font-size:12px; color:#9a9ca6; } .decide .grow { flex:1; min-width:240px; }
 .decide .chk { flex-direction:row; align-items:center; gap:6px; color:#eceef3; font-size:13px; }
 select, input.note { background:#1b1c1f; color:#eceef3; border:1px solid #36373d; border-radius:6px; padding:7px 9px; font:inherit; }
</style></head><body>
<header><h1>Rule case review</h1><span class="muted" id="prog"></span><span class="sp"></span>
 <button class="sec" id="imp">Import decisions…</button><input type="file" id="file" accept=".json" hidden>
 <button id="exp">Export decisions</button></header>
<main>
<p class="intro">__N__ rule cases decide how every FAA feature is labeled on the map. For each one, check the examples and the regulation,
pick the label, optionally add a note, and tick <b>Reviewed</b>. Then click <b>Export decisions</b> and save <code>rule_decisions.json</code>
in the project folder. The build applies it permanently (to existing data: <code>build_uas_layers.py --out out_full --renormalize</code>, then rebuild the map).
Regulation text from the eCFR, current as of __ECFR__.</p>
__CARDS__
</main>
<script>
const ORIG = __ORIG__;
const cards = [...document.querySelectorAll(".card")];
function prog() { const n = cards.filter(c => c.querySelector(".ok").checked).length;
  cards.forEach(c => c.classList.toggle("done", c.querySelector(".ok").checked));
  document.getElementById("prog").textContent = n + " of " + cards.length + " reviewed"; }
cards.forEach(c => c.querySelector(".ok").onchange = prog); prog();
document.getElementById("exp").onclick = () => {
  // keep everything already in rule_decisions.json (flags, resolutions, citations) and update the decisions
  const out = JSON.parse(JSON.stringify(ORIG || {}));
  const dec = out.decisions || (out.decisions = {});
  for (const c of cards) {
    const eff = c.querySelector(".eff-sel").value, note = c.querySelector(".note").value.trim(), ok = c.querySelector(".ok").checked;
    const prev = dec[c.dataset.case];
    if (!prev && !ok && eff === c.dataset.default && !note) continue;
    const d = dec[c.dataset.case] = { ...(prev || { key: c.dataset.case }) };
    if (d.decision && d.decision !== eff && !d.changed_from_default) d.changed_from_default = d.decision;
    d.decision = eff; d.note = note || undefined; d.reviewed = ok;
    if (ok) { d.reviewed_on = new Date().toISOString().slice(0, 10); delete d.new_case; }
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 2)], { type: "application/json" }));
  a.download = "rule_decisions.json"; a.click();
};
document.getElementById("imp").onclick = () => document.getElementById("file").click();
document.getElementById("file").onchange = async e => {
  const d = JSON.parse(await e.target.files[0].text());
  const dd = d.decisions || d;
  for (const c of cards) { const x = dd[c.dataset.case]; if (!x) continue;
    c.querySelector(".eff-sel").value = x.decision || x.effect; c.querySelector(".note").value = x.note || ""; c.querySelector(".ok").checked = !!x.reviewed; }
  prog();
};
</script></body></html>"""

if __name__ == "__main__":
    main()
