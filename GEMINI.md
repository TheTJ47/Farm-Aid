# GEMINI.md — instructions for AI coding agents (Antigravity / Gemini)

You are the coding agent for **FarmAid**, a research-prototype web app that
uses Sentinel-2 + Google Earth Engine to screen vegetation stress for a
study area in Maharashtra, India. Academic demonstration only — NOT a
certified crop-loss or insurance-adjudication system.

## Non-negotiable rules
1. **Inspect before editing.** Read the repo and the relevant files first.
   Never rebuild a working component; reuse code.
2. **Never fabricate results.** Do not claim a map, a real-data run, or a
   report download works unless you have actually run it. Demo (synthetic)
   outputs must never be presented as satellite observations.
3. **Small milestones, run + report.** After each milestone run the relevant
   tests and commands, then report the exact command, the actual output, and
   any remaining failure. Update `TASKS.md` and `CHANGELOG.md`.
4. **No paid services.** Free tiers only (noncommercial Earth Engine,
   GitHub, Streamlit Cloud). No paid APIs, subscriptions, or AI credits.
5. **No secrets in the repo.** Never commit credentials, tokens, or private
   data. `.env` and runtime artifacts are gitignored.
6. **Work backwards from the small.** Smallest implementation that can be
   demonstrated and tested; keep code understandable to a student with basic
   Python knowledge.
7. If an existing file or feature is missing or broken, **explain that
   before adding or replacing it.**

## Architecture (preserve it)
- **Stage 0 — Run identity:** `src/runmeta.py` — every assessment gets a
  run_id and a parameter fingerprint (mode, geometry, dates, baseline
  years, thresholds). Any input change → new fingerprint → previous
  results are stale and must never be presented/overlaid as current.
- **Stage 1 — Data:** `src/fetch_gee.py` — the AOI ALWAYS follows the
  selected geometry (selection + `aoi_buffer_km`); config `aoi_center` is
  only the default start. Live: Sentinel-2 via Earth Engine, cached by
  fingerprint. Demo: deterministic synthetic rasters FOR THE SELECTED AOI
  (valid CRS, labelled, never a fallback for live).
- **Stage 2 — Analysis:** `src/analyze.py::run_grid(cfg, procdir, outdir)`
  analyses ONLY the run's rasters; `src/field.py::assess_field` scores the
  exact selected geometry with evidence states ("insufficient data" vs
  "no evidence of damage" vs assessed). Hail unsupported → "unknown".
- **Stage 3 — Delivery:** `app/dashboard.py` (single state machine:
  selection → fetch → analysis → results; stale-labelling; failed runs
  clear results) and `src/report.py` (per-run PDFs, run-stamped).

## Rules specific to the selection workflow
- Never show a fixed grid, fixed patches or a previous run's overlays for
  the current selection.
- Imagery footprint layer ≠ basemap imagery; label both.
- A drawn/entered boundary is an assessment area, not proof of ownership;
  affected area is a screening estimate; snow/water/terrain-shadow are
  data limitations, not crop damage.

## Commands
```bash
pip install -r requirements.txt
earthengine authenticate          # once, real-data mode (user's own setup)
python -m pytest -q               # fully offline (testpaths=tests)
python src/fetch_gee.py --demo --center <lat> <lon>   # demo for a location
python src/fetch_gee.py --center <lat> <lon>          # live, after auth
python src/analyze.py
python src/field.py --coords <lat> <lon> <size_km>    # demo field run
python src/field.py --coords <lat> <lon> <size_km> --live
python src/report.py              # region evidence PDF
python tests/test_ee_connection.py  # MANUAL live EE integration check
streamlit run app/dashboard.py
```
Live data states are distinguished with precise errors: `no_matching_
imagery`, `cloud_filter_rejects_all`, `insufficient_clear_pixels` (post-
masking), or `valid`. Fallbacks (`baseline_fallback_days`,
`event_fallback_days`, `min_baseline_years`, `min_period_coverage_pct`)
are config-gated and the ACTUAL windows/years used are recorded in
source.json and reports. Live mode never falls back to synthetic data
and never reuses stale rasters.
Normal pytest must never require Earth Engine auth, network, or
credentials: the live check lives in `tests/test_ee_connection.py`,
guarded by `if __name__ == "__main__":`, and runs only when invoked
explicitly. `pytest.ini` restricts collection to `tests/`.
The AOI for every command follows the selection (`--center` geometry or
the dashboard's active boundary); `config.yaml` `aoi_center` is only the
default start location and is never modified by the project.

## Honesty
- Severity is provisional screening, not crop-loss %. Anomalous grid cells
  are "analytical units / pseudo-fields", never farm parcels.
- Real-data blockers (Earth Engine auth, cloud cover) are reported honestly,
  with the verified offline demo retained.