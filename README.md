# 🛰️ FarmAid — Field Damage Screening (selection-driven)

> "From satellite photos to claim-ready evidence in minutes."
> Research prototype (MDM — AI & DS) · ₹0 budget · **no ML training** ·
> academic demonstration, not insurance adjudication.

FarmAid assesses the field **you** select — anywhere, not a fixed study
site. The selection drives everything:

```
you choose geometry (coords box / drawn polygon)
   → AOI = selection + aoi_buffer_km
   → rasters for THAT AOI (live Sentinel-2 via Earth Engine, cached |
      deterministic synthetic demo for that location)
   → grid + field analysis of THOSE rasters
   → metrics, map layers, per-run GeoJSON/CSV/JSON/PDF
```

Every run has a **run id + parameter fingerprint** (mode, geometry, dates,
baseline years, thresholds). Change any input → previous results are
marked stale and are never overlaid or reported as current; a failed run
clears the previous result so no old PDF is presented.

---

## Windows PowerShell — full runbook

```powershell
cd "C:\Users\bagal\Desktop\MIT\# VII Sem\Minor Project\farm-aid"

# 1) Activate the existing virtual environment
.\venv\Scripts\Activate.ps1        # (or .\.venv\Scripts\Activate.ps1)

# 2) Install / update requirements
python -m pip install -r requirements.txt

# 3) Automated tests (fully offline — no Earth Engine needed)
python -m compileall -q src app tests
python -m pytest -q

# 4) Offline demo — synthetic rasters FOR THE SELECTED LOCATION
python src\fetch_gee.py --demo --center 28.2654 83.8203
python src\analyze.py
python src\field.py --coords 28.2654 83.8203 1.0

# 5) Region PDF + dashboard
python src\report.py
streamlit run app\dashboard.py

# 6) EXPLICIT live Earth Engine integration check (manual; your project id)
python tests\test_ee_connection.py
```

Your local `config.yaml` (including `gee_project`) is **never modified,
overwritten or read back into the project** — the ZIP does not contain it.
New settings (`aoi_buffer_km`, `min_valid_coverage_pct`) have code
defaults, so an older config.yaml keeps working as-is; the template
documents them.

## Farmer workflow (dashboard)

1. Choose **demo / live** mode (labelled everywhere; demo is synthetic,
   deterministic, and generated for the selected area — never displayed as
   satellite imagery).
2. Enter coordinates (Apply) **or** draw a rectangle/polygon on the map —
   drawing replaces the active boundary and clears the previous run.
3. Select observation dates + baseline years.
4. Inspect the boundary, its area, and the **imagery footprint (AOI)** on
   the map (only basemap + boundary + footprint are shown before a run —
   no fixed grid, no prerecorded patches).
5. Click **Run assessment** — real status, clear errors on empty imagery,
   cloudy windows, export failure or invalid geometry.
6. Metrics, severity-grid and field-result layers (this run only), before/
   after quicklook, download PDF/GeoJSON/JSON.
7. Move to another location and run again — a new AOI, new rasters, new
   run, new report.

**Honest limits (stated in-app and in reports):** drawn boundary ≠
ownership; affected area is a screening estimate; hail unsupported
(→ cause "unknown"); snow/terrain-shadow/water/non-agriculture are data
limitations — anomalies there are not interpreted as crop damage, and
insufficient coverage is reported as *insufficient data*, never as "no
damage" or as damage.

## Live data states (never conflated)
Live processing reports one of four states with a precise message:
1. **no_matching_imagery** — zero Sentinel-2 scenes in the window over the
   selected AOI (a genuine data gap; change dates/location).
2. **cloud_filter_rejects_all** — scenes exist but all exceed
   `max_cloud_pct`; raising that threshold is YOUR explicit decision.
3. **insufficient_clear_pixels** — scenes pass, but post-masking valid
   coverage is below `min_period_coverage_pct`.
4. **valid** — imagery available and analysis proceeds.
Transparent fallbacks (config-gated, actual dates/years recorded and shown
in reports): `baseline_fallback_days` / `event_fallback_days` widen a
failed window by ±N days; `min_baseline_years` lets unusable baseline
years be skipped and logged instead of aborting the whole run. Live mode
never substitutes synthetic data and never reuses stale rasters.

## Files

```
farm-aid/                  # ZIP extracts DIRECTLY here (flat layout)
├── README.md, GEMINI.md, TASKS.md, CHANGELOG.md
├── config.example.yaml    # template only — config.yaml stays yours
├── pytest.ini, requirements.txt, .gitignore
├── src/
│   ├── runmeta.py         # run ids + parameter fingerprints (staleness)
│   ├── fetch_gee.py       # AOI-from-selection, live (cached) / demo
│   ├── analyze.py         # grid analysis of the run's rasters
│   ├── field.py           # field assessment of the exact selection
│   └── report.py          # region + per-field PDFs (run-stamped)
├── app/dashboard.py       # selection→fetch→analysis→report state machine
└── tests/                 # offline suite + manual live EE check
```

## Commands (generic)

```bash
pip install -r requirements.txt
python -m pytest -q                       # offline
python src/fetch_gee.py --demo --center <lat> <lon>
python src/analyze.py
python src/field.py --coords <lat> <lon> <size_km> [--live]
python src/report.py
streamlit run app/dashboard.py
python tests/test_ee_connection.py        # manual live check (your auth)
```