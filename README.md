# Drone Airspace Map

A map of U.S. drone rules from public FAA data. Click anywhere on the map to see what applies there.

## How to run it (Windows)

**1. Install Python (one time)**

- Go to **https://www.python.org/downloads/** and click the yellow **Download Python** button.
- Open the installer. At the bottom, **tick "Add python.exe to PATH"**, then click **Install Now**.

**2. Get this folder onto your computer**

- If someone sent you a zip file: right-click it, choose **Extract All**, and open the extracted folder.

**3. Double-click `RUN_MAP.bat`**

- A black window opens. **Keep it open while you use the map** (it powers the Refresh buttons).
- The first time, it sets things up and downloads the FAA data. **This takes about 30 minutes.**
- Then the map opens in your browser. Next time it opens right away.

**4. Use the map**

- Click **Layers** (bottom left) and tick a state, for example **New York**.
- Click anywhere on the map to see the rules for that spot.

**5. Update the data**

- The bar at the top of the map shows how old the FAA data is.
- Click **Refresh TFRs** (a few minutes) or **Refresh all** (about 30 minutes). The map reloads by itself when done.
- This only works when the map was opened with `RUN_MAP.bat`. Opening `index.html` directly shows the map, but not the refresh.

**If something goes wrong:** take a screenshot of the black window and send it to whoever gave you the map.

---

## Publish it on Posit Connect Cloud (connect.posit.cloud)

1. Sign in at **connect.posit.cloud** with GitHub.
2. Click **Publish** > **Streamlit**, pick this repository and the **main** branch.
3. Primary file: **`streamlit_map.py`**. Dependencies: **`requirements.txt`**.
4. Open **Advanced settings** > **Add variable**: name `MAP_PASSWORD`, value = the password viewers will type.
5. Click **Publish**. Share the link and the password.

Every push to the repository republishes it automatically.
The free plan only publishes from a **public** repository; a private repository needs a paid plan.

## Publish it on Posit Connect (company server, password protected)

It is published as a **Streamlit** app: a password screen, then the map. Viewers pick states in the sidebar. The built map is already in this repository, so no download is needed to publish it.

1. Install Python (see step 1 above).
2. Double-click **`DEPLOY_TO_CONNECT.bat`** and answer three questions:
   - your Posit Connect address, for example `https://connect.yourcompany.com`
   - a Connect **API key** (in Connect: click your name, top right, then **API Keys**; you need Publisher rights)
   - the **password** people will type to open the map
3. In Connect, open **Drone Airspace Map**, go to **Settings > Access**, and choose **Anyone - no login required**. The password page protects it.

To change the password later: Connect > the app > **Settings > Vars** > `MAP_PASSWORD`.

On a Mac (Terminal, in this folder):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install rsconnect-python
.venv/bin/python deploy/build_site.py streamlit_app
export MAP_PASSWORD='the password'
.venv/bin/rsconnect deploy streamlit streamlit_app --entrypoint app.py --title "Drone Airspace Map" -E MAP_PASSWORD \
  --server https://YOUR-CONNECT-URL --api-key YOUR-API-KEY
```

To publish newer data: refresh it with `RUN_MAP.bat` (or pull the latest version of this repository), then run `DEPLOY_TO_CONNECT.bat` again.
The Refresh buttons do not work on the hosted copy; the data age bar still shows how old it is.

---

## For advanced users

Same steps as commands (PowerShell, from this folder):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python build_uas_layers.py --out out_full
.venv\Scripts\python make_v2_full.py
.venv\Scripts\python serve_map.py
```

| Task | Command |
|---|---|
| Try the Streamlit (hosted) version locally | `$env:MAP_PASSWORD='test'; .venv\Scripts\streamlit run streamlit_app\app.py` |
| Open the map with working Refresh buttons | `.venv\Scripts\python serve_map.py` (serves the map at http://127.0.0.1:8765) |
| Refresh only TFRs and stadium events (fast) | `.venv\Scripts\python build_uas_layers.py --out out_full --group fast`, then `make_v2_full.py` |
| Add OpenStreetMap infrastructure (NY, KY, VA) | `.venv\Scripts\python osm_infra_pull.py` (add `--resume` if a step failed), then `make_v2_full.py` |
| Review the labeling rules | `.venv\Scripts\python make_rule_review.py`, open `review\rule_review.html`, export `rule_decisions.json` into this folder, then `build_uas_layers.py --out out_full --renormalize` and `make_v2_full.py` |
| Publish the Flask version instead of Streamlit | `deploy\build_site.py`, then `rsconnect deploy flask deploy --entrypoint app:app -E MAP_PASSWORD ...` |

Data: FAA UAS Data Delivery System, FAA SEAMS, tfr.faa.gov, OpenStreetMap (© OpenStreetMap contributors).
Reference only: check B4UFLY or LAANC before flying.
