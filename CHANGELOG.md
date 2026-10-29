# CHANGELOG.md

## v0.4.9 — Satellite-based crop damage insurance assessment core fixes (2026-10-26)
**Status: fully executed and verified in Windows PowerShell environment. All 81 automated tests passing (1 skipped for live EE check).**

### Key Findings & Fixes
- **1. Universal `claim_info` Support & Keyword Guard (`src/field.py`)**:
  - Fixed `TypeError: assess_field() got an unexpected keyword argument 'claim_info'` across all execution paths.
  - Added `claim_info=None, **kwargs` to `assess_field` signature and updated CLI argument parser in `src/field.py::main()` to accept `--claim-ref`, `--policy-no`, `--claimant-name`, `--crop-type`, `--peril`, and `--incident-date`.
- **2. Date & Observation-Window Harmonization (`src/incident.py`, `src/field.py`, `src/storage.py`, `src/report.py`, `app/dashboard.py`)**:
  - Removed artificial `-7 days` reference date alteration in `src/incident.py::discover_incident_scenes()`. The user's claimed incident date is strictly preserved without mutation or fabrication.
  - In recent mode, scene discovery chronologically pairs the latest suitable pass (`selected_post`) with the closest prior suitable pass (`selected_pre`).
  - Added exact observation-window metadata (`[selected_pre['date'], selected_post['date']]`) and search window metadata to `fetch_incident_rasters`, `fetch_incident_demo`, `metrics`, `save_assessment`, `load_active_assessment`, `build_field_report`, and dashboard display tables.
  - Synchronized `param_key` computation in `assess_field` with `app/dashboard.py` for incident comparison workflows (`[incident_date, workflow_mode, claimed_peril]`).
- **3. Damage Calculation & Denominator Audit (`src/field.py`, `src/analyze.py`)**:
  - Confirmed strict separation between regional grid results (300m cells across buffered landscape in `src/analyze.py`) and parcel-level results (clipped exact polygon in `src/field.py`).
  - Verified parcel pixel count (`n_field_pixels`, calculated via rasterio geometry mask) is the denominator for `valid_pct`.
  - Verified valid pixel count (`n_valid`, co-located pixels finite and non-nodata in both event and baseline) is the denominator for `affected_pct`.
  - Verified estimated affected area scales true parcel planar area (`area_ha`) by `frac` (the fraction of anomalous valid pixels).
  - Enhanced `evidence_state` to robustly classify `no_evidence` when qualifying anomalies are below 0.5% with severity `none`.
- **4. Evidentiary Insurance Report & Uncertainty Principles (`src/report.py`)**:
  - Re-anchored Section 1 and Section 2 around claimed incident dates and actual satellite acquisition dates with explicit day offsets.
  - Rewrote uncertainty explanations: explicitly articulated that 100% clear satellite imagery confirms optical observation completeness, but does NOT equate to 100% certainty of legal cause or crop loss without ground verification.
  - Added peril-specific Loss Adjuster Verification Checklists in Section 7 for Inundation/Flood, Drought/Dryness, Hail/Storm, and General agronomic management cross-checks.
  - Clarified hail limitations: optical sensors detect acute defoliation but cannot prove hail impact without Doppler radar storm tracking or on-site panchnama.
- **5. Regression Tests (`tests/test_pipeline.py`)**:
  - Added `test_assess_field_accepts_unexpected_kwargs_and_claim_info`.
  - Added `test_incident_date_and_observation_window_consistency`.
  - Added `test_damage_calculation_denominators_and_metrics`.
  - Added `test_field_report_with_claim_info_and_peril_checklist`.
  - Full test suite passes: **81 passed, 1 skipped**.

## v0.4.8 — Post-assessment dashboard loading & synchronization fix (2026-10-26)
**Status: fully executed and verified in Windows PowerShell environment. All 77 automated tests passing (1 skipped for live EE check).**

### Key Findings & Fixes
- **Root Cause Investigation**:
  - Investigated intermittent failure of dashboard to automatically display analysis immediately after "Run before-and-after assessment":
    1. *Staleness Discrepancy*: `canonical_settings(eff)` used the user-configured `max_cloud` during assessment run, whereas the dashboard's staleness check evaluated `canonical_settings(cfg)` using `config.yaml` default (60%). When `max_cloud != 60`, `run_is_current` evaluated to `False`, displaying a stale results warning and suppressing the results section.
    2. *Folium Drawing Desynchronization*: In `st_folium`, drawn geometries from previous Leaflet interactions triggered `new_geom.wkt != sel.wkt` checks even in `Coordinates (box)` mode or due to floating-point vertex permutations, resetting `st.session_state[RUN] = None` and forcing a blank rerun.
    3. *Separated Loading Paths*: Manual saved-run loading in Tab 2 read directly from disk, whereas the automatic post-run path relied on volatile session state without a dedicated synchronization loader.
- **Unified Loading Architecture (`src/storage.py`, `app/dashboard.py`)**:
  - Implemented `storage.load_active_assessment(run_id)`: unified loader used by both automatic post-assessment completion and manual saved-run reopen actions.
  - Enhanced `storage.save_assessment` to persist `workflow_mode`, `incident_date`, `claimed_peril`, and `discovery` scene records.
  - Introduced explicit `ACTIVE_RUN_ID` and `LAST_SUCCESS_MSG` in session state to maintain lockstep synchronization between run ID, artifacts, and UI notifications.
  - Restricted Leaflet draw event processing strictly to `input_mode == "Draw on map"`, using Shapely topological `not curr_geom.equals(new_geom)` instead of fragile WKT string comparison.
  - Harmonized `canonical_settings(eff_cfg)` with active UI `max_cloud` so post-run results are immediately recognized as current.
  - Updated Results Section in Tab 1 to render whenever an active run is present; if input parameters change, a prominent warning is displayed without suppressing the active analysis.
- **Regression Tests (`tests/test_pipeline.py`)**:
  - `test_consecutive_runs_and_active_run_activation`: verifies that consecutive runs immediately and automatically activate the newest run.
  - `test_manual_saved_run_loading`: verifies manual loading recovers all metrics, artifacts, and claim details.
  - `test_artifact_loading_failure_prevents_silent_stale_display`: verifies corrupted/missing runs return `None` and fail safely without showing stale analysis.
  - `test_param_key_consistency_with_user_settings`: verifies parameter key consistency with custom user settings.
  - Full test suite passes: **77 passed, 1 skipped**.

## v0.4.6 — Live run audit, GeoTIFF nodata masking fix, and real calculation verification (2026-10-26)
**Status: fully executed and verified in Windows PowerShell environment. All 66 automated tests passing (1 skipped for live EE check). Verified against live run `run_20261026T043351_512dc0`.**

### Key Findings & Bug Fixes
- **Live Run Audit (`run_20261026T043351_512dc0`)**:
  - Investigated the divergence between the regional grid (164/169 cells flagged, 97%) and the selected field report (0.276 ha, 0.0% affected, screening level 'None', z = +1.12, ΔNDVI = +0.150).
  - **Identified Root Cause (Bug #1 — Unmasked NODATA in Regional Grid)**:
    - Earth Engine exports GeoTIFFs with masked cloud/shadow pixels unmasked to `-9999.0` (NODATA). However, GEE `getDownloadURL` does not embed the GeoTIFF TIFFTAG_GDAL_NODATA metadata, causing `src.nodata` in rasterio to return `None`.
    - `src/analyze.py::read_masked` previously checked `if src.nodata is not None: return np.ma.masked_where(arr == src.nodata, arr)`. Because `src.nodata` was `None`, `-9999.0` pixels were treated as valid floating-point numbers rather than masked nodata.
    - This corrupted 43 of the 169 grid cells, injecting astronomical negative anomalies (NDVI deltas down to -9221.98, z-scores down to -190,040), falsely flagging all unmasked cloud pixels as crop damage and triggering false "drought" classifications.
    - Simultaneously, `n_valid` counted `-9999.0` as valid pixels, erroneously reporting `100.0%` valid coverage even for cells with 92% cloud cover.
    - In contrast, `src/field.py::read_field_rasters` already checked `(arr == nodata_val) | ~np.isfinite(arr)` with fallback `nodata_val = -9999.0`, so the field report was never corrupted by -9999.
  - **Identified Agronomic & Spatial Divergence (Reason #2 — Field vs Landscape)**:
    - The farmer-drawn field is a 0.276 ha parcel (45 pixels) located in Kollam, Kerala. During August–September 2025, that specific plot was vigorously green (mean NDVI 0.657 vs historical baseline 0.507, ΔNDVI = +0.150, z = +1.12), exhibiting zero damage.
    - Across the surrounding 4km × 4km landscape (169 grid cells of 9 ha each), regional NDVI exhibited a modest seasonal dip (regional mean NDVI 0.638 vs baseline 0.717, ΔNDVI = -0.078). Because multi-year baseline standard deviation is low (mean std = 0.054), even a -0.08 dip crosses the `z < -1.5` threshold. With cell severity triggering at `affected_pct >= 5%`, 164 of 169 cells correctly identify localized stress in the broader region while the farmer's field inside Cell 85 remains healthy.
- **Calculation & Masking Fixes (`src/analyze.py`, `src/fetch_gee.py`, `src/field.py`)**:
  - Fixed `src/analyze.py::read_masked` to always mask `(arr == nodata_val) | (arr == NODATA) | ~np.isfinite(arr)`, regardless of whether the GeoTIFF header contains the nodata tag.
  - Fixed `src/analyze.py::mask_nodata` to mask both nodata and non-finite values.
  - Updated `src/fetch_gee.py::download_tiff` to explicitly stamp `dst.nodata = NODATA` onto downloaded GeoTIFFs using rasterio.
  - Updated `src/analyze.py::run_grid` to use `n_valid = int(v_z.count())` (evaluating pixels where both event and baseline are valid) and added concise regional diagnostics (`dNDVI`, `z`, severity breakdown).
  - Updated quicklook rendering in `src/analyze.py` with `plt.cm.YlGn.with_extremes(bad="#EBEBEB")` and `plt.cm.YlOrRd.with_extremes(bad="#EBEBEB")`, cleanly rendering cloudy/missing data in neutral grey.
  - Replaced unicode Delta in console outputs with ASCII `dNDVI` for Windows cp1252 terminal compatibility.
  - Streamlit compliance: verified all dataframes, images, and download buttons use `width="stretch"`.
- **Regression Tests (`tests/test_pipeline.py`)**:
  - `test_read_masked_handles_nodata_none`: validates nodata masking when GeoTIFF `nodata=None`.
  - `test_run_grid_masks_nodata_cleanly`: validates `run_grid` output on rasters containing -9999 without tag, ensuring no negative NDVI, runaway z-scores, or false drought.
  - `test_field_vs_regional_divergence_logic`: validates that a healthy field (0% affected, z > 0) is correctly reported even when embedded in a landscape with negative anomalies.
  - Suite now has **66 passed, 1 skipped**.
- **Real Results Verification**:
  - Re-ran `run_grid` on `run_20261026T043351_512dc0`.
  - `damage_summary.csv` now reflects real satellite physical values: NDVI mean range [0.372, 0.776], z-score range [-11.22, +1.62], 0 cells with corrupted negative NDVI, and 43 cells honestly reflecting valid coverage < 100%.

## v0.4.5 — live imagery bug fix, farmer-friendly UI redesign, and scientific validation (2026-10-26)
**Status: fully executed and verified in Windows PowerShell environment. All 63 automated tests passing (1 skipped for unauthenticated live Earth Engine check). Full browser workflow verified via subagent.**

### Key Enhancements & Bug Fixes
- **Live Imagery & Masking Audit (`src/fetch_gee.py`)**:
  - Diagnosed root cause of the `0.0% / 0.1% valid pixels remain` error: in monsoon months (August–mid September) across western Maharashtra, Sentinel-2 scenes have heavy cloud cover (>80–100%); only 2 scenes passed the 60% filter. In the previous run `run_20261025T201654_ccc8ec`, coverage was checked over the 4km AOI (which had 60.3% clear pixels in surrounding areas), while the 1.9 ha field itself was 100% obscured by cloud shadows (SCL 3) and clouds (SCL 8/9).
  - Preserved regional AOI coverage verification for raster downloads while ensuring field-level clipping in `field.py` captures actual field coverage (triggering the `INSUFFICIENT DATA` evidence state instead of crashing with an uncaught abort).
  - Executed live Earth Engine assessment `run_20261025T212717_8db277` with real Sentinel-2 Level-2A imagery, verifying end-to-end composite downloads, field clipping, and graceful evidence state handling.
  - Added per-scene metadata extraction (`scenes_diag`: ID, UTC timestamp, whole-tile `CLOUDY_PIXEL_PERCENTAGE`, and local `aoi_clear_pct`) via `reduceRegion` over AOI for each passing scene, distinguishing whole-granule tile cloud cover from local study area clear percentage.
  - Added `img.unmask(NODATA)` before `.clip(region)` in `download_tiff` so masked pixels are cleanly exported as `-9999.0`, never conflated with valid 0.0 NDVI.
  - Synchronized `eff["baseline_window"]` to calendar month/day of the user's selected event dates (`[d_start.strftime("%m-%d"), d_end.strftime("%m-%d")]`), eliminating cross-season baseline mismatches.
  - Replaced all 8 deprecated `use_container_width=True` calls with `width="stretch"` in `app/dashboard.py` for Streamlit modern compliance.
  - Enhanced `period_error_message` for `insufficient_clear_pixels` to provide plain-language guidance suggesting clear windows (e.g. post-monsoon).
- **Field Geometry Clipping & Denominator Accuracy (`src/field.py`)**:
  - `read_field_rasters(..., geom=geom)` clips and geometry-masks the rasters to the exact farmer's boundary using `rasterio.mask.mask` with `all_touched=True`.
  - `score_arrays` accepts and calculates `valid_pct` against `n_field_pixels` (the field's pixels) rather than the surrounding 172,000+ pixels of the AOI.
  - Redesigned `make_field_quicklook` with modern farmer-friendly titles, clean colorbars, grey background for masked/cloud pixels, and removed axis ticks.
  - Cleaned colormap deprecation warning with `.with_extremes(bad="#EBEBEB")`.
  - Updated `geometry_from_inputs` to handle flexible positional arguments and kwargs (`lat`, `lon`, `size_km`).
- **Farmer-Friendly UI Redesign (`app/dashboard.py`)**:
  - Completely restructured results view into 4 clear sections:
    - **Section A — Field screening summary**: Clean metric cards for Field Area, Usable Satellite Data, Area with Possible Stress, Estimated Area Flagged, Screening Level, and Confidence Status.
    - **Section B — What does this mean?**: Contextual, plain-language answers and actionable recommendations.
    - **Section C — Field map**: Distinct layer names (Selected field boundary, Satellite image coverage area, Regional screening grid, Areas flagged for possible crop stress) with an inline color legend.
    - **Section D — Satellite evidence & visuals**: Three panels (Typical vegetation in previous years, Vegetation during selected period, Locations flagged for possible stress) and collapsible technical details.
  - Prominently displays `🔴 LIVE — real Earth Engine satellite observations` vs `🔵 DEMO — synthetic example data, not actual satellite observations`.
  - Fixed active boundary confusion: shows exact active field details (centroid, area in ha & acres, source) and warns when sidebar coordinates are modified but not yet applied.
  - Documented rectangle side semantics (1.0 km = 100 ha = 247 ac; 0.10 km = 1 ha; 0.064 km ≈ 1 acre).
- **Report Alignment (`src/report.py`)**:
  - Aligned field report table indicators with farmer-friendly labels ("Field area", "Usable satellite data", "Area showing possible crop stress", "Estimated area with possible stress", "Screening level", "Possible cause").
  - Cautious phrasing avoiding claims that flood/drought is definitively ruled out from satellite alone.
  - Mode line prominently distinguishes LIVE vs DEMO.
- **Automated Test Coverage**:
  - Added tests for rectangle size semantics and UTM area math.
  - Added tests for `score_arrays` with field pixel clipping denominator (`n_field_pixels`).
  - Added tests for mixed positive and negative anomaly pixels.
  - Added tests for severity levels and plain-language insights.
  - Total test count expanded from 59 to **63 passed, 1 skipped**.

**Status: fully executed and verified in Windows PowerShell environment. All 59 automated tests passing (1 skipped for unauthenticated live Earth Engine check). Full browser workflow verified via subagent.**

### Key Enhancements & Bug Fixes
- **Assessment Persistence (`src/storage.py`)**:
  - Automatically saves all completed assessment runs to `data/saved_assessments/{run_id}.json`.
  - Persists full provenance: parameter key, processing mode, observation windows, baseline years, geometry WKT, metrics, and artifact paths.
  - Implements `save_assessment`, `list_saved_assessments`, and `load_saved_assessment`.
- **Saved Runs & Multi-Assessment Comparison**:
  - Added dedicated **Saved Runs & Compare** tab in `app/dashboard.py`.
  - Provides tabular overview of all saved assessments with run ID, mode, area (ha/ac), affected %, severity, cause, and acquisition windows.
  - Interactive drill-down to inspect any previous run, view quicklooks, and download artifacts.
  - Multi-run comparison selector (up to 6 runs) with automatic mismatch detection and warnings when observation windows, baseline years, or processing modes differ.
- **Plain-Language Assessment Insights (`src/report.py::generate_field_insights`)**:
  - Automatically translates complex satellite metrics (NDVI delta, anomaly z-scores, valid pixel coverage, affected percentage) into readable, transparent explanations.
  - Explicit handlers for evidence states: distinguishes insufficient data (data limitation, cloud masking, sensor gaps) from no evidence of damage.
  - Contextual interpretations for flood (elevated NDWI) and drought (persistent negative NDVI anomaly).
  - Integrated into both the field report PDF generator (Section 4) and the Streamlit dashboard expander.
- **Dual Unit Support**:
  - Consistent presentation of agricultural areas in both hectares (ha) and acres (ac) throughout the UI, reports, and comparison tables (`HA_TO_ACRES = 2.47105`).
- **Defect Fixes & Compatibility**:
  - Fixed test assertion case mismatch (`test_pipeline.py`).
  - Fixed numpy bool/scalar preservation in Pandas GeoDataFrame serialization (`field.py::map_safe_geodataframe`).
  - Replaced deprecated `datetime.utcnow()` and `datetime.utcfromtimestamp()` with timezone-aware `datetime.now(datetime.UTC)` and `datetime.fromtimestamp(..., tz=timezone.utc)`.
- **Automated Test Coverage**:
  - Added tests for `generate_field_insights` across insufficient data, no evidence, flood cause, and normal stress scenarios.
  - Added round-trip persistence tests for `storage.py`.
  - Added conversion tests for hectare-to-acre calculations.
  - Total test count expanded from 52 to **59 passed, 1 skipped**.

## v0.4.3 — live baseline diagnostics, three data states, transparent fallback (2026-10-25)
**Status: code written; NOT executed in this session (no Python runner).
User-confirmed failure: live processing aborted with the generic
'No images matched for baseline 2021 08-01..09-15' message.**

### Root cause
`fetch_from_gee` ran one binary check (raw collection size after the
cloud filter) per period and aborted on the FIRST empty baseline year. The
message conflated three very different situations: genuinely no scenes
over the AOI/window; scenes existed but ALL exceeded `max_cloud_pct`;
or scenes passed but post-masking clear pixels were too few. There was no
diagnostic trail (collection id, AOI, raw vs cloud-rejected counts, actual
dates) and no way to skip a bad baseline year or widen a window
transparently.

### Fix
- **Per-period diagnostics** (`query_and_describe`): AOI, collection,
  raw image count, cloud-rejected count, first/last acquisition dates,
  actual window used (fallback-aware) — all recorded in `source.json`
  and printed.
- **Three explicit data states** via pure classifier `diagnose_period`:
  `no_matching_imagery` / `cloud_filter_rejects_all` /
  `insufficient_clear_pixels` (`min_period_coverage_pct` checked AFTER
  masking) — plus `valid`. Errors carry `FarmAidDataError.state` and a
  precise, actionable message. The cloud filter is NEVER raised
  automatically.
- **Transparent fallback (config-gated, recorded)**: `baseline_fallback_days`
  / `event_fallback_days` widen a failed window by ±N days (0 = strict);
  `min_baseline_years` allows skipping and recording unusable baseline
  years as long as the minimum remains; reports list the ACTUAL years and
  windows used plus skipped years.
- **No synthetic fallback**: live mode raises; verified by a regression
  test that proves `fetch_demo` is never invoked when Earth Engine is
  unavailable.
- Reports (`build_report`/`build_field_report`) now render the actual
  baseline years used and skipped years, not just the configured list.

### Tests added (none weakened)
`test_diagnose_period_states`, `test_period_error_messages_carry_state`,
`test_window_fallback_widening_recorded`,
`test_baseline_shortfall_message_lists_reasons`,
`test_live_mode_never_falls_back_to_demo`, plus an OPTIONAL real-query
test (`test_live_small_baseline_query_real`) that skips automatically
when Earth Engine credentials are unavailable. Expected suite: 55 passed
+ 1 optional (skipped without creds) — to be confirmed by the user's run.

## v0.4.2 — dashboard runtime crash fix: ndarray GeoJSON properties (2026-10-25)
**Status: code written; NOT executed in this session (no Python runner).
User-confirmed error: `TypeError: Object of type ndarray is not JSON
serializable` from folium at `app/dashboard.py` (former line 270) when
serializing the field-result layer in demo mode.**

### Root cause
`field_result.geojson` carries array/list-valued properties
(`bounds_wgs84`, `centroid_wgs84`). Re-reading the file with
geopandas/pyogrio brings those back as **numpy ndarrays**, and folium
serializes layer features with `json.dumps(gdf.__geo_interface__)`, which
cannot encode ndarray or numpy-scalar values (non-finite floats would
also fail/serialize badly).

### Fix (display-only, authoritative results untouched)
- `src/field.py`: added `geojson_safe_value()` (recursive: ndarray →
  list, numpy scalars → native Python, tuple → list, dict recursed,
  non-finite float → None) and `map_safe_geodataframe(gdf, drop_columns)`
  which returns a JSON-clean COPY for the map; geometry and meaningful
  assessment attributes are preserved; the authoritative
  field_result.geojson/metrics.json are never mutated. The authoritative
  GeoJSON write also passes every metric through `geojson_safe_value`.
- `app/dashboard.py`: both run-result layers (severity grid and assessed
  field) are built from `map_safe_geodataframe`; internal columns
  (`geometry_wkt`, `param_key`) are omitted from the map layer only.

### Tests added (assertions not weakened)
- `test_map_geodataframe_sanitizes_ndarray_properties` — reproduces the
  exact crash (GeoDataFrame with ndarray property + numpy scalars +
  NaN/Inf) and proves `json.dumps(__geo_interface__)` succeeds with exact
  conversions (arrays→lists, True kept bool, NaN/Inf→None, internal
  columns dropped, geometry/attributes kept).
- `test_map_geodataframe_readback_like_field_result` — simulates the
  dashboard read-back of field_result.geojson and proves full
  serialization with preserved values.
- Expected suite: 49 passed (47 + 2 new) — to be confirmed by the user's
  run (compileall + pytest + demo dashboard are not executable here).

## v0.4.1 — hotfix: report.py IndentationError + ee import guard (2026-10-25)
**Status: code written; NOT executed in this session (no Python runner).
User-reported baseline after their temp indentation fix: 46 passed,
1 failed (`test_live_init_error_is_clear`).**

### Fixed
- `src/report.py::build_field_report` — orphaned "Data source &
  acquisition" list-fragment (leftover from an edit) caused an
  `IndentationError` at the former line 282. Replaced with proper
  `story.append(...)` calls; the section now flows story → 1. Field
  identity → 2. Data source & acquisition → 3. Field indicators →
  quicklook → 4. Method & limitations.
- `src/fetch_gee.py::init_ee` — `import ee` now wrapped in
  try/except: a missing `earthengine-api` package (ModuleNotFoundError /
  ImportError) becomes a clear `FarmAidDataError` with install guidance;
  live mode never falls back to synthetic data. This is the exact path
  `test_live_init_error_is_clear` exercises.
- Expected suite: 47 passed (was 46+1) — to be confirmed by the user's run.

## v0.4.0 — selection-driven engine (2026-10-25)
**Status: code written; NOT executed in this session (no Python runner).
User verification required per TASKS.md.**

### Root causes addressed (from the user's functional audit)
1. **Fixed geography baked in** — the config centre drove raster extent,
   the grid and the dashboard overlays; selection never influenced the
   AOI. Now: AOI = selected geometry + `aoi_buffer_km` everywhere
   (`fetch_gee.selection_to_aoi`); the config centre is only the default
   start location.
2. **Dashboard had no state machine** — drawing and Run did not share one
   active geometry; input changes did not invalidate prior results. Now:
   one geometry in session state; drawing replaces + invalidates; every
   run carries a parameter fingerprint (`runmeta.param_key`); stale
   results are banner-labelled and never overlaid; failed runs clear the
   previous result.
3. **Demo was not spatial** — one fixed scenario at the config centre.
   Now: deterministic per-AOI generation (seed = AOI), valid
   UTM georeferencing, guaranteed flood + drought patch types; moving the
   selection moves the synthetic scene; source.json records the geometry.
4. **Region grid / severity not tied to the run** — `analyze.run_grid`
   now analyses ONLY the run's rasters; the dashboard renders the grid
   only from the current run's outputs.
5. **No run identity** — artifacts, GeoJSON, JSON, PDF and map overlays all
   carry the same `run_id`; PDF filename includes it; stale outputs cannot
   be mistaken for the new run.
6. **Silent partial coverage** — field metrics now separate "insufficient
   data" (valid coverage < `min_valid_coverage_pct`) from "no evidence of
   damage"; affected area is always labelled a screening estimate; snow/
   terrain-shadow/water limitations documented in report + app.

### Added
- `src/runmeta.py` (run ids, parameter fingerprints, staleness predicate).
- Tests: param-key sensitivity; AOI follows selection (incl. Himalayan
  coords); demo regenerates per location + deterministic per location;
  stale-rasters rejection; evidence states; grid-derived-from-rasters;
  graceful EE-error (offline monkeypatch); different-location acceptance
  (two runs → different geometry/AOI/results).
- Dashboard: basemap labels (OSM/Esri — basemap ≠ observations), imagery
  footprint layer, run-keyed layers, run provenance JSON, stale banners.

### Preserved / not weakened
- 9% affected fixture semantics, valid-pixel denominators, severity/cause
  thresholds, hail-unsupported rule, balance + georeferencing regression
  tests, empty-imagery guards, offline-only normal pytest.

## v0.3.3 — georeferencing fix (2026-10-25)
**Status: code written; NOT executed here (no Python runner in this
session). User verification required: compileall + pytest + offline run.**

### Root cause fixed (real defect, not a test issue)
- `src/fetch_gee.py::fetch_demo` built the raster transform from
  `from_origin(west, north, SCALE, SCALE)` where west/north were
  LONGITUDE/LATITUDE DEGREES, while the rasters were tagged with a UTM
  (metres) CRS. Field geometries projected into UTM could therefore never
  map into the raster pixels. Now the degree AOI is projected into the UTM
  zone (`pyproj.Transformer`) and the transform is built from the projected
  bounds with the 10 m pixel size; raster dims/transform/CRS are mutually
  consistent. `source.json` (demo) now records `projected_bounds_m` and
  the `grid` shape.
- `src/field.py::crop_field_demo` previously caught only `ValueError` on
  disjoint windows; Rasterio raises `rasterio.errors.WindowError`. The
  field-window computation and intersection are now inside one
  try/except that converts `WindowError`/`ValueError` (and any
  non-overlapping result) into a clear `FarmAidDataError`.

### Tests (not weakened; two added)
- `test_demo_rasters_georeferencing` — asserts UTM CRS, 10 m pixels, and
  raster extent ≈ projected AOI (±1.5 px).
- Existing fixture correction (6x6 = 9%), valid-pixel denominator test,
  far-outside-geometry test, and metrics/JSON/PDF consistency checks all
  retained.
- Expected suite: 36 tests (incl. parametrized severity) — to be confirmed
  by the user's run.

## v0.3.2 — fix two confirmed offline test failures (2026-10-25)
**Status: code written; requires execution on the user's Windows machine
(compile + pytest + offline workflow).**

### Fixed
- `test_score_arrays_basic` (expected ~9%, function returned 4%): the text
  fixture `_arrays_for_field()` created a 4x4 damaged block (16/400 px = 4%)
  while the documented definition and assertion said 6x6 (36/400 = 9%).
  The scoring code was correct; the fixture now creates the 6x6 block the
  definition intends. Test not weakened.
- `test_assess_field_demo_end_to_end`: `src/field.py::crop_field_demo`
  called `win.intersection(src.window)` — `src.window` is a method, not a
  Window object. Now intersects against `src.window(*src.bounds)` and
  handles non-overlapping fields explicitly (`FarmAidDataError` with a
  clear message; also catches rasterio builds that raise on disjoint
  windows).

### Added (boundary coverage requested)
- `test_crop_field_demo_outside_raises` — a field ~150 km outside the demo
  region raises `FarmAidDataError`, not a cryptic rasterio error.
- `test_score_arrays_denominators` — valid-pixel coverage and affected
  fraction both use the VALID-pixel denominator (nodata rows excluded).
- End-to-end test now asserts persisted `field_metrics.json` agrees with
  the returned metrics that feed the PDF and the GeoJSON.

## v0.3.1 — fix, verify, deliver clean ZIP (2026-10-25)
**Status: code written; execution runs on the user's Windows machine.**
All changes below target the specific failures found in the v0.3 ZIP test.

### Fixed
- **pytest collection error A** — `tests/test_ee_connection.py` refactored:
  `ee` imported only inside `main()` under `if __name__ == "__main__":`;
  `gee_project` resolved from `config.yaml` via a cwd-independent path;
  clear error message when the project id is missing. Normal `pytest -q`
  is fully offline (no auth/network required).
- **pytest duplicate module error B** — ZIP now has a FLAT layout (files at
  archive root, no `farm-aid/` wrapper) so extraction over an existing
  project never creates a nested tree; new `pytest.ini` sets
  `testpaths = tests` + `norecursedirs`, so stale nested copies are never
  collected.
- **Syntax error** — `app/dashboard.py`: malformed multiline f-string in
  `st.spinner(...)` replaced by a conditional string variable.
- **`.count()` on plain arrays** — `field.score_arrays` now normalises all
  inputs via `np.ma.asarray` + `masked_invalid`; masked and ordinary numpy
  array handling is correct.
- **Geometry test** — no longer compares degree spans of lon/lat; asserts
  projected area range (~100 ha for a 1 km box).
- **End-to-end test dir contract** — demo rasters written to
  `<root>/<data_dir>/processed` (where field assessment reads them).
- **Config safety** — ZIP ships `config.example.yaml` ONLY; existing
  `config.yaml` is preserved on extraction; `load_config()` auto-creates
  config.yaml from the template when missing (`shutil.copyfile`).
- Demo-imbalance regression test bounds tightened and grounded in the
  generator math (affected share ~8–12% of the demo scene).

## v0.3.0 — farmer field workflow + reliability fixes (2026-10-25)
**Status: code written; NOT executed in this session. First verification
happens in Antigravity per TASKS.md (some v0.2.0 items verified by user run).**

### Added
- `src/field.py` — farmer-facing field assessment: boundary selection
  (coordinates + size, or drawn/imported GeoJSON polygon), live Earth
  Engine query CLIPPED to the boundary, demo crop of synthetic rasters,
  shared z-score scoring, outputs `field_result.geojson`,
  `field_metrics.json`, `field_quicklook.png` (3-panel before/after) and
  `reports/FarmAid_field_report.pdf` (per-field PDF via
  `report.build_field_report`). CLI: `python src/field.py --coords ...`
  and `--geojson ...` with `--live`.
- Dashboard field workflow: sidebar boundary input (coordinates or draw on
  map), observation-window + baseline-year pickers, in-app assessment run,
  field metrics cards, est. affected area (labelled screening estimate),
  quicklook, PDF download button, last-result map overlay + popup.
- Tests: `test_score_arrays_*`, `test_area_ha_range`,
  `test_validate_geometry`, `test_geometry_from_inputs`,
  `test_field_report_pdf`, `test_assess_field_demo_end_to_end`,
  `test_demo_outputs_balanced` (regression for the imbalance bug).
- `config.yaml`: `field:` section (default_size_km, hail_classification:
  unsupported — documented decision).

### Fixed
- **Skewed synthetic outputs (602/676 cells high severity, 598 flood)** —
  root cause: demo patch radii were in scene-fraction units (~60% of the
  scene). Now in pixels (10 m/px), plus drought patches; balanced mix.
- **Centroid geographic-CRS warnings** — centroids computed in projected
  CRS (EPSG:326xx) then reprojected to 4326.
- **Streamlit `use_container_width` deprecations** removed from
  dataframe/image calls.
- Reportlab report: `build_report` refactored with `build_field_report`;
  shared styles/tables; fixed undefined `mode` variable.

## v0.2.0 — spec-alignment pass (2026-10-25)
**Status: code written; NOT executed in this session (no Python/Earth Engine
environment here). First verification happens in Antigravity per TASKS.md.**

### Added
- `GEMINI.md` (agent instruction rules), `TASKS.md`, `CHANGELOG.md`
- `src/report.py` — ReportLab evidence report generated from current
  outputs (`reportlab` added to requirements)
- `tests/test_pipeline.py` — NDVI/NDWI math, nodata handling, zero-std
  guard, severity thresholds, cause rules, empty-collection guard,
  GeoJSON round-trip, PDF generation (`pytest` added to requirements)
- Acquisition metadata sidecar `data/processed/source.json` (mode, image
  counts, date ranges, CRS, resolution, valid-pixel coverage)

### Changed
- `fetch_gee.py`: cloud mask now uses Sentinel-2 SCL (cloud shadows,
  clouds, cirrus, defective pixels) OR'ed with QA60 bits; explicit
  empty-collection guard (`FarmAidDataError`) with actionable message;
  NDWI definition and SR band-scaling documented; demo mode writes the
  same `source.json` interface with `synthetic: true`; no silent demo
  fallback on failed real requests (explicit `--demo` only)
- `analyze.py`: pure functions extracted for testability (`ndvi`, `ndwi`,
  `anomaly_z`, `mask_nodata`, `classify_severity`, `classify_cause`);
  per-cell fields added: `ndvi_delta`, `z_score`, `valid_pct`,
  `affected_pct` (screening indicator, not crop-loss %); severity mapped
  to none/low/moderate/high (config thresholds); cause now
  flood/drought/unknown with an uncertainty label; hail explicitly
  unsupported → unknown
- `dashboard.py`: layer controls (severity grid / negative-anomaly cells /
  OSM / satellite basemap), mode badge (demo vs live from source.json),
  methodology + data-source sections, `st.warning` disclaimer, valid-data
  coverage metric, cell popups with statistics
- `config.yaml`: Rahuri (Maharashtra) illustrative AOI, severity thresholds
  (low/moderate/high), `min_valid_pixels`, `max_cloud_pct`
- `.gitignore`: ignores runtime rasters, source.json, outputs

## v0.1.0 — initial scaffold (2026-10-25)
- Baseline: README, config.yaml, requirements.txt, src/fetch_gee.py,
  src/analyze.py, app/dashboard.py — working structure with demo mode