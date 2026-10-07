# Drone Airspace Map

A map of U.S. drone rules from public FAA data. Click anywhere on the map to see what applies there.

## How to run it (Windows)

**1. Install Python (one time)**

- Go to **https://www.python.org/downloads/** and click the yellow **Download Python** button.
- Open the installer. At the bottom, **tick "Add python.exe to PATH"**, then click **Install Now**.

**2. Get this folder onto your computer**

- If someone sent you a zip file: right-click it, choose **Extract All**, and open the extracted folder.

**3. Double-click `RUN_MAP.bat`**

- A black window opens. Leave it open.
- The first run sets things up, then downloads the FAA data. **This takes about 30 minutes.**
- When it's done, the map opens in your browser.

**4. Use the map**

- Click **Layers** (bottom left) and tick a state, for example **New York**.
- Click anywhere on the map to see the rules for that spot.

Next time, just open **`v2_full\index.html`**. Double-click `RUN_MAP.bat` again whenever you want fresh data.

**If something goes wrong:** take a screenshot of the black window and send it to whoever gave you the map.

---

## For advanced users

Same steps as commands (PowerShell, from this folder):

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python build_uas_layers.py --out out_full
.venv\Scripts\python make_v2_full.py
start v2_full\index.html
```

| Task | Command |
|---|---|
| Refresh only TFRs and stadium events (fast) | `.venv\Scripts\python build_uas_layers.py --out out_full --group fast`, then `make_v2_full.py` |
| Add OpenStreetMap infrastructure (NY, KY, VA) | `.venv\Scripts\python osm_infra_pull.py` (add `--resume` if a step failed), then `make_v2_full.py` |
| Review the labeling rules | `.venv\Scripts\python make_rule_review.py`, open `review\rule_review.html`, export `rule_decisions.json` into this folder, then `build_uas_layers.py --out out_full --renormalize` and `make_v2_full.py` |
| Publish to Posit Connect with a password | see `deploy\app.py` (password in the `MAP_PASSWORD` setting on Connect, never in code) |

Data: FAA UAS Data Delivery System, FAA SEAMS, tfr.faa.gov, OpenStreetMap (© OpenStreetMap contributors).
Reference only: check B4UFLY or LAANC before flying.
