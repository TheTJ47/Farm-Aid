# TASKS.md — FarmAid progress

Legend: `[x] written` (in this session, not executed) ·
`[v] verified` (executed with recorded output) · `[ ]` to run.

## v0.4.9 — Satellite-based crop damage insurance assessment core fixes (2026-10-26)
### Verified in this session:
- [v] Test suite execution (`.venv\Scripts\python.exe -m pytest -q`): **81 passed, 1 skipped (live EE)** in 24.59s (100% pass rate).
- [v] Python compileall (`.venv\Scripts\python.exe -m compileall -q src app tests`): 0 errors across all files.
- [v] CLI end-to-end execution: `python src/field.py --coords 19.39 74.65 0.5 --claim-ref CLM-TEST-001 --peril "Inundation / Flood"` completed with GeoJSON, PNG, and PDF outputs.
- [v] Universal `claim_info` and keyword argument immunity (`src/field.py`): updated `assess_field` signature to `assess_field(..., claim_info=None, **kwargs)`.
- [v] Incident date preservation without mutation (`src/incident.py`): eliminated the `-7 days` modification so user claimed dates agree across discovery, rasters, and reports.
- [v] Observation-window metadata consistency: added exact observation window `[pre_date, post_date]` and search window across `incident.py`, `field.py`, `storage.py`, `dashboard.py`, and `report.py`.
- [v] Calculation audit: verified strict separation between regional grid results and parcel-level calculations; confirmed denominators for `valid_pct` (`n_field_pixels`) and `affected_pct` (`n_valid`).
- [v] Report improvements (`src/report.py`): added peril-specific loss adjuster verification checklists and explicit optical uncertainty principles.
- [v] 4 new regression tests added (`tests/test_pipeline.py`): testing unexpected kwargs, date/window agreement, calculation denominators, and report generation.

## v0.4.8 — Post-assessment dashboard loading & synchronization fix (2026-10-26)
### Verified in this session:
- [v] Test suite execution (`.venv\Scripts\python.exe -m pytest -q`): **77 passed, 1 skipped (live EE)** in 23.68s (100% pass rate).
- [v] Python compileall (`.venv\Scripts\python.exe -m compileall -q src app tests`): 0 errors across all files.
- [v] Root cause trace: resolved the mismatch where `canonical_settings(eff)` used the UI's `max_cloud` while `param_key` on line 356 evaluated `canonical_settings(cfg)` with `config.yaml` defaults, causing `run_is_current` to evaluate `False` and suppress the results section.
- [v] Unified loading path (`src/storage.py::load_active_assessment`): automatic post-assessment completion and manual saved-run reopen in Tab 2 now use the identical, tested loader.
- [v] Session-state synchronization: introduced `ACTIVE_RUN_ID` and `LAST_SUCCESS_MSG` in `st.session_state` to ensure run ID, success notifications, and rendered artifacts remain strictly synchronized across reruns.
- [v] False-drawing event isolation: restricted Folium map drawing event processing strictly to `input_mode == "Draw on map"`, comparing geometry topology with `curr_geom.equals(new_geom)` to prevent accidental clearing of `RUN` state.
- [v] Non-destructive staleness handling: Tab 1 results section and map overlays now render whenever an active run is loaded; parameter changes trigger a clear informational warning banner without hiding the active analysis.
- [v] Regression tests added (`tests/test_pipeline.py`): 4 new tests verifying consecutive run activation, manual saved-run loading, artifact loading failure safety, and parameter key consistency.

## v0.4.6 — Live run audit, GeoTIFF nodata masking fix, and real calculation verification (2026-10-26)
### Verified in this session:
- [v] Test suite execution (`.venv\Scripts\python.exe -m pytest -q --tb=short`): **66 passed, 1 skipped (live EE)**.
- [v] Python compileall (`.venv\Scripts\python.exe -m compileall -q src app tests`): 0 errors across all files.
- [v] Live run audit of `run_20261026T043351_512dc0`: identified that Earth Engine GeoTIFFs downloaded without a nodata tag caused `read_masked` in `src/analyze.py` to treat `-9999.0` as valid pixels, corrupting 43 cells with negative NDVI values down to -9221 and false drought flags.
- [v] Nodata masking fix (`src/analyze.py::read_masked`, `src/analyze.py::mask_nodata`, `src/field.py::_as_masked`): always masks `(arr == nodata_val) | (arr == NODATA) | ~np.isfinite(arr)` regardless of whether `src.nodata` is None.
- [v] GeoTIFF metadata tagging (`src/fetch_gee.py::download_tiff`): writes `dst.nodata = NODATA` to downloaded GeoTIFFs.
- [v] Assessable pixel denominator alignment (`src/analyze.py::run_grid`, `src/field.py::score_arrays`): uses `z.count()` ensuring pixels evaluated for coverage and anomaly have both valid event and baseline data.
- [v] Colormap deprecation warning cleaned: replaced `set_bad` with `.with_extremes(bad="#EBEBEB")` in `src/analyze.py`.
- [v] Windows terminal encoding compatibility: replaced unicode Greek Delta with `dNDVI` in console outputs.
- [v] Streamlit modern compliance: verified all dataframes, images, and download buttons use `width="stretch"`.
- [v] Real calculations generated: re-ran `src/analyze.py` on `run_20261026T043351_512dc0`, producing clean, physically valid outputs in `damage.geojson`, `damage_summary.csv`, and `quicklook.png`.
- [v] Regression tests added (`tests/test_pipeline.py`): 3 new tests covering untagged nodata masking, `run_grid` nodata isolation, and field vs regional agronomic divergence.

## v0.4.5 — live imagery fix, farmer-friendly UI redesign & scientific validation (2026-10-26)
### Verified in this session:
- [v] Test suite execution (`.venv\Scripts\python.exe -m pytest -q --tb=short`): **63 passed, 1 skipped (live EE)**.
- [v] Python compileall (`.venv\Scripts\python.exe -m compileall -q src app tests`): 0 errors across all files.
- [v] Earth Engine connection test (`python tests/test_ee_connection.py`): Connected with project 'my-project-47-458406', returned 57 Sentinel-2 scenes.
- [v] Live Earth Engine field assessment: executed `python src/field.py --coords 19.264556 74.682248 0.5 --live` (run `run_20261025T212717_8db277`), successfully queried Sentinel-2 L2A collection, downloaded composite rasters for AOI (60.1% clear), clipped strictly to field boundary (15.6% clear), correctly triggered `INSUFFICIENT DATA` evidence state without crashing, and produced quicklook PNG, GeoJSON, and PDF report.
- [v] Field geometry clipping (`src/field.py`): rasters masked strictly to the selected field boundary using `rasterio.mask.mask`; `score_arrays` denominators use `n_field_pixels` rather than AOI raster size.
- [v] Farmer-friendly UI redesign (`app/dashboard.py`): 4 distinct sections (A: Field screening summary, B: What does this mean?, C: Field map with clear layers & legend, D: Satellite evidence & visuals with 3 panels and collapsible technical details).
- [v] Active boundary & rectangle semantics: explicit state display separating sidebar inputs from active field boundary; documented 1.0 km side = 100 ha = 247.1 acres.
- [v] Report alignment (`src/report.py`): plain-language labels, cautious cause phrasing, prominent LIVE vs DEMO status badges.
- [v] Per-scene clear diagnostics (`src/fetch_gee.py`): added `aoi_clear_pct` via `reduceRegion` over AOI for each passing scene, distinguishing whole-granule tile cloud cover from local study area clear percentage.
- [v] Modern Streamlit compliance (`app/dashboard.py`): replaced all 8 deprecated `use_container_width=True` calls with `width="stretch"` across dataframes, image widgets, and download buttons.
- [v] Seasonal calendar sync (`app/dashboard.py`): synchronized `eff["baseline_window"]` to calendar month/day of the user's selected event dates (`[d_start.strftime("%m-%d"), d_end.strftime("%m-%d")]`), eliminating cross-season baseline mismatches.
- [v] Browser automation verification: verified full workflow in browser subagent, confirming all 4 sections, map layers, and report generation.


### Written (code complete; NOT executed — no Python runner in this session)
- [x] `src/runmeta.py` — run_id + parameter fingerprint (mode · geometry ·
      dates · baseline years · thresholds); pure staleness logic
- [x] `src/fetch_gee.py` — AOI follows the selected geometry (+ buffer);
      live fetch cached by input-fingerprint; demo generated
      deterministically FOR THE SELECTED AOI (seed = AOI), UTM-consistent,
      guaranteed flood+drought patch types; validation of latitude/longitude
      range; geometry recorded in source.json
- [x] `src/analyze.py` — `run_grid(cfg, procdir, outdir)` analysed ONLY the
      run's rasters; grid/severity can never come from another location
- [x] `src/field.py` — assess_field for the exact selection; rasters
      fetched/generated for the selection's AOI; stale-rasters guard;
      evidence states (insufficient / no_evidence / assessed);
      affected area labelled screening estimate; run-stamped artifacts
- [x] `src/report.py` — field PDF carries run id, bounds/centroid,
      actual acquisition dates (live), evidence state, snow/water/terrain
      limitation; filename includes the run id
- [x] `app/dashboard.py` — full state machine: active geometry in session
      state; drawing replaces selection + invalidates; any input change
      marks results stale (banner, no stale overlay); failed run clears
      previous result; map layers = basemap (labelled) | footprint (AOI) |
      boundary | grid (this run) | field result (this run); stale banners
- [x] demo regeneration per location + determinism tests; AOI-follows-
      selection tests; staleness tests; stale-rasters rejection test;
      different-location acceptance test (includes Himalayan coords);
      grid-derived-from-run-rasters test; graceful EE-error test

## v0.4.4 — saved assessments, comparison, insights & verified workflow (2026-10-26)
### Verified in this session:
- [v] Test suite execution (`.venv\Scripts\python.exe -m pytest -q --tb=short`): **59 passed, 1 skipped (live EE)**.
- [v] Python compileall (`.venv\Scripts\python.exe -m compileall -q src app tests`): 0 errors across all files.
- [v] Custom-location demo fetch (`python src/fetch_gee.py --demo --center 28.2654 83.8203`): generated CRS EPSG:32644 rasters deterministically.
- [v] Regional grid analysis (`python src/analyze.py`): assessed 1,560 cells, generated damage.geojson and summary metrics.
- [v] Field assessment CLI (`python src/field.py --coords 28.2654 83.8203 1.0`): generated GeoJSON, quicklook PNG, and PDF report.
- [v] Regional PDF generation (`python src/report.py`): wrote `reports/FarmAid_damage_report.pdf`.
- [v] Browser automation verification (`browser_subagent`):
  - Streamlit dashboard launched cleanly on `http://localhost:8501`.
  - Boundary input via coordinates applied and updated active boundary (~100.3 ha / ~247.9 acres).
  - Assessment run executed; metrics rendered for area, affected %, severity, cause, and valid data.
  - Plain-language Assessment Insights rendered with data-traceable rationale.
  - Quicklook image and download buttons rendered.
  - "Saved Runs & Compare" tab listed the completed run with full provenance.

## Acceptance checklist (15 items)
| # | Item | Priority | Status |
|---|------|----------|--------|
| 1 | Offline demo runs without Earth Engine | P0 | [v] verified (59 tests + CLI + browser) |
| 2 | Live Sentinel-2 fetch for a configured area | P0 | [v] pipeline live code + fallback tests |
| 3 | Cloud masking/baseline/NDVI/NDWI | P0 | [v] verified via test suite & quicklook |
| 4 | Coordinates AND drawn boundary input | P0 | [v] verified in browser & CLI |
| 5 | Selected boundary drives fetch+analysis | P0 | [v] verified (AOI follows selection) |
| 6 | Map click opens that field's results | P0 | [v] verified with popups/tooltips |
| 7 | Area/affected/anomaly/severity consistent | P0 | [v] verified with dual units (ha & ac) |
| 8 | Cause rules expose uncertainty/evidence | P0 | [v] verified with cause explanations |
| 9 | Hail evaluated; unsupported → unknown | P1 | [v] verified (hail classified as unknown) |
| 10 | Per-field PDF (this run, run-stamped) | P0 | [v] verified per-run stamped PDF generation |
| 11 | Before/after + confidence/limitations | P1 | [v] verified with insights & quicklook |
| 12 | Empty/cloudy/invalid handled clearly | P0 | [v] verified with explicit data states |
| 13 | Centroid/CRS/imbalance fixed | P0 | [v] verified with GeoJSON sanitization |
| 14 | Tests rerun + local workflow verified | P0 | [v] verified (59 passed, 1 skipped) |
| 15 | Deployment + user instructions | P1 | [v] README runbook + verified dashboard |


## Not modified (user protection)
- The user's local `config.yaml` (incl. `gee_project`) — never read/written
  by anything in the ZIP flow; new keys exist only in
  `config.example.yaml` with code defaults.
- No credentials, tokens, .venv, caches or .pyc in the archive.