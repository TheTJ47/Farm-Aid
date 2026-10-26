#!/usr/bin/env python3
"""FarmAid — Field Damage Screening Dashboard (Farmer-Friendly UI Redesign).

Academic research prototype: Sentinel-2 satellite observations vs multi-year healthy
seasonal baselines to screen for possible crop stress in farmer-selected boundaries.

Sections:
  Section A: Field Screening Summary (Key metrics, screening level, confidence)
  Section B: What Does This Mean? (Plain-language guidance and practical next steps)
  Section C: Field Map (Selected boundary, satellite coverage area, regional grid, stress areas)
  Section D: Satellite Evidence & Technical Details (Visual comparison, downloads, provenance)
"""
import copy
import datetime as dt
import json
import os
import sys

import folium
import geopandas as gpd
import numpy as np
import pandas as pd
import streamlit as st
from folium.plugins import Draw
from shapely.geometry import box as make_box
from shapely.geometry import shape as to_shape
from streamlit_folium import st_folium

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from analyze import run_grid  # noqa: E402
from fetch_gee import (FarmAidDataError, load_config,  # noqa: E402
                       selection_to_aoi)
import field as fieldmod  # noqa: E402
from field import map_safe_geodataframe, validate_geometry  # noqa: E402
from report import generate_field_insights, HA_TO_ACRES  # noqa: E402
from runmeta import canonical_settings, new_run_id, param_key, \
    results_are_current  # noqa: E402
from storage import (DEFAULT_SAVED_DIR as SAVED_DIR,  # noqa: E402
                     list_saved_assessments, load_saved_assessment,
                     save_assessment)

# Distinct screening colors
COLORS = {
    "none": "#2E8B57",       # Green: No significant stress detected
    "low": "#B9C93B",        # Yellow-green: Low screening level
    "moderate": "#E8A33D",   # Orange: Moderate screening level
    "high": "#C22D2D",       # Red: High screening level
    "unassessable": "#7A7A7A" # Grey: Insufficient satellite data
}

# --------------------------------------------------------------------------- #
# Page config
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="FarmAid — Satellite Field Screening",
    page_icon="🌱",
    layout="wide"
)

cfg = load_config(os.path.join(ROOT, "config.yaml"))

# --------------------------------------------------------------------------- #
# Custom Styling for Farmer Readability
# --------------------------------------------------------------------------- #
st.markdown("""
<style>
    .metric-card {
        background-color: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 8px;
        padding: 14px 18px;
        margin-bottom: 10px;
    }
    .metric-title {
        font-size: 0.85rem;
        color: #64748b;
        font-weight: 600;
        text-transform: uppercase;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: #0f172a;
    }
    .metric-sub {
        font-size: 0.82rem;
        color: #475569;
        margin-top: 4px;
    }
    .badge-live {
        background-color: #dc2626;
        color: white;
        padding: 4px 10px;
        border-radius: 9999px;
        font-size: 0.8rem;
        font-weight: 600;
        display: inline-block;
    }
    .badge-demo {
        background-color: #0284c7;
        color: white;
        padding: 4px 10px;
        border-radius: 9999px;
        font-size: 0.8rem;
        font-weight: 600;
        display: inline-block;
    }
</style>
""", unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Session state initialisation
# --------------------------------------------------------------------------- #
SEL = "sel"          # dict: {"geom": shapely, "wkt": str, "source": str, "applied_coords": tuple}
RUN = "run"          # dict: run result + param_key + inputs used
DRAW = "draw_seq"    # counter to detect new drawings

for key, default in [(SEL, None), (RUN, None), (DRAW, 0)]:
    if key not in st.session_state:
        st.session_state[key] = default

# --------------------------------------------------------------------------- #
# Sidebar: User Controls & Active Boundary Status
# --------------------------------------------------------------------------- #
st.sidebar.title("🌱 FarmAid")
st.sidebar.caption("Satellite screening for your specific field boundary.")

mode = st.sidebar.radio(
    "Data mode",
    ["demo", "live"],
    format_func=lambda m: "🔵 DEMO (Synthetic simulation)" if m == "demo"
    else "🔴 LIVE (Copernicus Sentinel-2 satellite observations)",
    help="Live mode connects to Earth Engine for actual satellite observations. "
         "Demo mode runs an offline deterministic simulation for testing."
)

st.sidebar.divider()
st.sidebar.subheader("1. Field boundary")

input_mode = st.sidebar.radio(
    "How to set boundary",
    ["Coordinates (box)", "Draw on map"]
)

coords_modified = False
if input_mode == "Coordinates (box)":
    lat = st.sidebar.number_input(
        "Latitude (center)", value=cfg["aoi_center"][0], step=0.001, format="%.4f")
    lon = st.sidebar.number_input(
        "Longitude (center)", value=cfg["aoi_center"][1], step=0.001, format="%.4f")
    size_km = st.sidebar.number_input(
        "Field square width (km)",
        value=cfg.get("field", {}).get("default_size_km", 1.0),
        min_value=0.05, max_value=10.0, step=0.05, format="%.2f",
        help="Width of square field: 1.0 km = 100 ha (247 ac); 0.10 km = 1 ha (2.47 ac); 0.064 km ≈ 1 acre."
    )
    st.sidebar.caption(f"Square side {size_km:.2f} km → creates a field of ~{(size_km*size_km*100):.1f} ha (~{(size_km*size_km*100*HA_TO_ACRES):.1f} acres).")

    # Check if sidebar coordinates differ from currently applied selection
    current_sel = st.session_state.get(SEL)
    if current_sel is not None and current_sel.get("source") == "coordinates":
        applied_coords = current_sel.get("applied_coords")
        if applied_coords != (lat, lon, size_km):
            coords_modified = True

    if coords_modified:
        st.sidebar.warning("⚠️ Inputs changed! Click below to apply to active boundary.")

    if st.sidebar.button("Apply coordinates → active boundary", type="secondary"):
        geom = fieldmod.geometry_from_inputs(coords=(lat, lon, size_km))
        st.session_state[SEL] = {
            "geom": geom, "wkt": geom.wkt, "source": "coordinates",
            "applied_coords": (lat, lon, size_km)
        }
        st.session_state[RUN] = None
        st.sidebar.success("Active boundary updated from coordinates.")
        st.rerun()
else:
    st.sidebar.info("✏️ Use the draw tools on the map (polygon or rectangle) to draw your field boundary.")

# Active boundary card in sidebar
st.sidebar.markdown("---")
st.sidebar.markdown("**Current active field:**")
active_sel = st.session_state.get(SEL)
if active_sel is None:
    st.sidebar.warning("No active boundary. Enter coordinates and click Apply, or draw on the map.")
else:
    ha_active = fieldmod.area_ha(active_sel["geom"])
    ac_active = round(ha_active * HA_TO_ACRES, 2)
    c_active = active_sel["geom"].centroid
    st.sidebar.success(
        f"**Source:** {active_sel['source'].capitalize()}\n\n"
        f"**Area:** {ha_active:.3f} ha (~{ac_active} acres)\n\n"
        f"**Center:** {c_active.y:.4f}°N, {c_active.x:.4f}°E"
    )

st.sidebar.divider()
st.sidebar.subheader("2. Observation dates")
d0 = dt.date.fromisoformat(cfg["event_window"][0])
d1 = dt.date.fromisoformat(cfg["event_window"][1])
d_start = st.sidebar.date_input("Observation start", value=d0)
d_end = st.sidebar.date_input("Observation end", value=d1)

year_options = list(range(2020, 2027))
years = st.sidebar.multiselect(
    "Baseline comparison years",
    year_options,
    default=[y for y in cfg["baseline_years"] if y in year_options],
    help="Historical healthy seasons used as the baseline comparison."
)

max_cloud = st.sidebar.number_input(
    "Max tile cloud filter (%)",
    value=cfg.get("max_cloud_pct", 60),
    min_value=10, max_value=100, step=5,
    help="Rejects satellite scenes with higher cloud cover across the 100km tile."
)

# Run button
st.sidebar.divider()
run_disabled = st.session_state[SEL] is None
if st.sidebar.button("🚀 Run screening for active field",
                     type="primary", disabled=run_disabled):
    geom = st.session_state[SEL]["geom"]
    try:
        geom = validate_geometry(geom)
    except FarmAidDataError as e:
        st.session_state[RUN] = None
        st.sidebar.error(str(e))
        geom = None

    if geom is not None:
        if not years:
            st.sidebar.error("Select at least one baseline comparison year.")
        elif d_end <= d_start:
            st.sidebar.error("Observation end date must be after start date.")
        else:
            eff = copy.deepcopy(cfg)
            eff["event_window"] = [d_start.isoformat(), d_end.isoformat()]
            eff["baseline_window"] = [d_start.strftime("%m-%d"), d_end.strftime("%m-%d")]
            eff["baseline_years"] = sorted(years)
            eff["max_cloud_pct"] = max_cloud
            current_key = param_key(mode, geom.wkt, eff["event_window"],
                                    eff["baseline_years"],
                                    canonical_settings(eff))
            run_id = new_run_id()
            with st.spinner("Screening field with satellite observations…"):
                try:
                    meta, procdir = fieldmod.prepare_rasters(
                        eff, geom, mode, ROOT, run_id)
                    grid_summary = run_grid(eff, procdir,
                                            os.path.join(
                                                ROOT, eff["out_dir"],
                                                run_id))
                    res = fieldmod.assess_field(
                        eff, geom, mode, root=ROOT, run_id=run_id,
                        rasters_dir=procdir, meta=meta)
                    run_data = {
                        "run_id": run_id,
                        "param_key": current_key,
                        "mode": mode,
                        "event_window": eff["event_window"],
                        "baseline_years": eff["baseline_years"],
                        "geom_wkt": geom.wkt,
                        "res": res,
                        "grid_summary": grid_summary,
                    }
                    st.session_state[RUN] = run_data
                    save_assessment(run_data)
                    st.sidebar.success(f"Assessment complete (Run ID: {run_id}).")
                except (FarmAidDataError, Exception) as e:  # noqa: BLE001
                    st.session_state[RUN] = None
                    st.sidebar.error(f"Assessment failed — {e}")

if st.sidebar.button("🗑️ Clear all and reset"):
    st.session_state[SEL] = None
    st.session_state[RUN] = None
    st.rerun()

# --------------------------------------------------------------------------- #
# Fingerprint & Staleness
# --------------------------------------------------------------------------- #
sel = st.session_state.get(SEL)
run = st.session_state.get(RUN)
current_key = None
if sel is not None:
    eff_dates = [d_start.isoformat(), d_end.isoformat()]
    eff_years = sorted(years) if years else cfg["baseline_years"]
    current_key = param_key(mode, sel["wkt"], eff_dates, eff_years,
                            canonical_settings(cfg))
run_is_current = (run is not None and current_key is not None
                  and results_are_current(run["param_key"], current_key))

# --------------------------------------------------------------------------- #
# Main Tabs
# --------------------------------------------------------------------------- #
tab_assess, tab_compare = st.tabs(["📊 Field Assessment", "📁 Saved Runs & Compare"])

# ========================================================================== #
# TAB 1: Assessment
# ========================================================================== #
with tab_assess:
    st.title("🌱 FarmAid — Satellite Crop Screening")
    badge_html = '<span class="badge-live">🔴 LIVE SATELLITE MODE</span>' if mode == "live" else '<span class="badge-demo">🔵 DEMO SIMULATION MODE</span>'
    st.markdown(f"{badge_html} &nbsp; **Sentinel-2 observations vs multi-year healthy seasonal norms**", unsafe_allow_html=True)
    st.caption("Provisional screening for academic research and demonstration — not certified crop loss insurance adjudication.")

    if run is not None and not run_is_current:
        st.warning("⚠️ The results shown below belong to a previous selection. Input parameters (dates, baseline, or boundary) have changed. Click 'Run screening' to update.")

    if sel is None:
        st.info("👋 Welcome! To begin, specify your field using coordinates in the sidebar (click 'Apply coordinates') or draw directly on the map below.")

    # ---------------------------------------------------------------------- #
    # Section C: Field Map
    # ---------------------------------------------------------------------- #
    st.subheader("Field map & satellite coverage")
    center = (cfg["aoi_center"][0], cfg["aoi_center"][1])
    zoom = 13
    if sel is not None:
        c = sel["geom"].centroid
        center = (c.y, c.x)
        ha = fieldmod.area_ha(sel["geom"])
        if ha < 2:
            zoom = 17
        elif ha < 20:
            zoom = 15
        elif ha < 150:
            zoom = 14

    m = folium.Map(location=center, zoom_start=zoom, tiles="OpenStreetMap",
                   name="Map · OpenStreetMap")
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        name="Basemap · Satellite (Esri Imagery)",
        attr="Esri Basemap — for visual context, not the analytical observations").add_to(m)

    # Layer 1: Selected field boundary
    if sel is not None:
        fg_sel = folium.FeatureGroup(name="① Selected field boundary (Orange)", show=True)
        folium.GeoJson(
            gpd.GeoDataFrame([{"geometry": sel["geom"]}], crs="EPSG:4326"),
            style_function=lambda f: {"color": "#FF6600", "weight": 4, "fillColor": "#FF6600", "fillOpacity": 0.15},
            tooltip="Selected field boundary",
        ).add_to(fg_sel)
        fg_sel.add_to(m)

        # Layer 2: Satellite image coverage area (AOI)
        aoi = selection_to_aoi(cfg, sel["geom"])
        fg_aoi = folium.FeatureGroup(name="② Satellite image coverage area (Blue dashed)", show=True)
        folium.Rectangle(
            bounds=[(aoi[1], aoi[0]), (aoi[3], aoi[2])],
            color="#2A4494", weight=2, fill=False, dash_array="6",
            tooltip="Satellite retrieval area (field + buffer)",
        ).add_to(fg_aoi)
        fg_aoi.add_to(m)

    # Layers 3 & 4 (Current run only)
    if run_is_current:
        res = run["res"]
        grid_geojson = os.path.join(res["outdir"], "damage.geojson")
        if os.path.exists(grid_geojson):
            fg_grid = folium.FeatureGroup(name="③ Regional screening grid (~300m cells)", show=False)
            gdf_ = map_safe_geodataframe(gpd.read_file(grid_geojson))
            def style_grid(feat):
                s = feat["properties"].get("severity", "none")
                col = COLORS.get(s, "#888888")
                return {"fillColor": col, "color": col, "weight": 1, "fillOpacity": 0.4}
            folium.GeoJson(
                gdf_, style_function=style_grid,
                tooltip=folium.GeoJsonTooltip(fields=["cell_id", "affected_pct", "severity", "cause"],
                                              aliases=["Cell", "Stress %", "Level", "Cause"])
            ).add_to(fg_grid)
            fg_grid.add_to(m)

        fg_field = folium.FeatureGroup(name="④ Areas flagged for possible crop stress", show=True)
        if os.path.exists(res["geojson"]):
            fgf = map_safe_geodataframe(gpd.read_file(res["geojson"]),
                                        drop_columns=("geometry_wkt", "param_key"))
            if not fgf.empty:
                folium.GeoJson(
                    fgf,
                    style_function=lambda f: {"fillColor": "#C22D2D", "color": "#7F1D1D", "weight": 3, "fillOpacity": 0.25},
                    tooltip="Assessed Field Screening Result"
                ).add_to(fg_field)
                fg_field.add_to(m)

    Draw(export=False, draw_options={
        "polyline": False, "polygon": True, "rectangle": True,
        "circle": False, "marker": False, "circlemarker": False,
    }).add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    # Render map
    col_map, col_legend = st.columns([4, 1])
    with col_map:
        map_out = st_folium(m, width="100%", height=480, key="farmaid_map",
                            returned_objects=["last_active_drawing"])
        if map_out and isinstance(map_out, dict):
            drawing = map_out.get("last_active_drawing")
            if drawing:
                g2 = drawing.get("geometry") if isinstance(drawing, dict) else None
                if isinstance(g2, str):
                    try: g2 = json.loads(g2)
                    except Exception: g2 = None
                if g2:
                    try:
                        new_geom = validate_geometry(to_shape(g2))
                        if new_geom.wkt != (st.session_state[SEL] or {}).get("wkt"):
                            st.session_state[SEL] = {
                                "geom": new_geom, "wkt": new_geom.wkt, "source": "drawn",
                                "applied_coords": None
                            }
                            st.session_state[RUN] = None
                            st.rerun()
                    except FarmAidDataError as e:
                        st.error(f"Drawing rejected: {e}")

    with col_legend:
        st.markdown("**Map layers & legend:**")
        st.markdown(
            "- 🟧 **Selected field**: your assessment area\n"
            "- 🟦 **Dashed box**: satellite retrieval footprint\n"
            "- 🟩 **Normal / Healthy**: no significant anomaly\n"
            "- 🟨 **Low stress level**: minor localized dip\n"
            "- 🟧 **Moderate stress level**: noticeable vegetation stress\n"
            "- 🟥 **High stress level**: widespread anomaly\n"
            "- ⬜ **Grey**: clouds, shadow, or missing data"
        )
        st.caption("Basemap is for geographic orientation; calculations use Sentinel-2 observations.")

    # ---------------------------------------------------------------------- #
    # Results Section (Only shown when run is current)
    # ---------------------------------------------------------------------- #
    if run_is_current:
        res = run["res"]
        m_ = res["metrics"]
        st.divider()

        # ================================================================== #
        # Section A: Field Screening Summary
        # ================================================================== #
        st.subheader("Section A: Field screening summary")

        if m_.get("insufficient_data"):
            st.error(
                f"⚠️ **Insufficient data for a reliable screening** — only {m_['valid_pct']}% of the field had usable satellite observations. "
                "Clouds, shadows, or satellite revisit gaps prevented a reliable view. This is a data limitation, NOT evidence of healthy crops."
            )
        elif m_.get("no_evidence_of_damage"):
            st.success("✓ **No evidence of significant crop stress detected** under current thresholds and available clear observations.")

        area_ha_v = m_["area_ha"]
        area_ac_v = round(area_ha_v * HA_TO_ACRES, 2)
        aff_ha_v = m_.get("affected_area_ha", 0)
        aff_ac_v = round(aff_ha_v * HA_TO_ACRES, 3)

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Field area</div>
                <div class="metric-value">{area_ha_v} ha</div>
                <div class="metric-sub">~{area_ac_v} acres inside boundary</div>
            </div>
            """, unsafe_allow_html=True)
        with c2:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Usable satellite data</div>
                <div class="metric-value">{m_['valid_pct']}%</div>
                <div class="metric-sub">{m_.get('n_valid_pixels', '?')} clear pixels analyzed</div>
            </div>
            """, unsafe_allow_html=True)
        with c3:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Area with possible stress</div>
                <div class="metric-value">{m_['affected_pct']}%</div>
                <div class="metric-sub">~{aff_ha_v} ha ({aff_ac_v} ac) screening estimate</div>
            </div>
            """, unsafe_allow_html=True)
        with c4:
            sev_label = m_["severity"].capitalize()
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Screening level</div>
                <div class="metric-value">{sev_label}</div>
                <div class="metric-sub">Algorithm anomaly classification</div>
            </div>
            """, unsafe_allow_html=True)

        c5, c6 = st.columns(2)
        with c5:
            cause_label = m_["cause"].capitalize()
            if m_["cause"] == "unknown":
                cause_text = "Cannot determine from satellite data alone"
            elif m_["cause"] == "flood":
                cause_text = "Waterlogging / Inundation signal (elevated moisture)"
            elif m_["cause"] == "drought":
                cause_text = "Persistent moisture deficit / Dryness pattern"
            else:
                cause_text = cause_label
            st.info(f"**Possible cause:** {cause_text}  \n*Indicator uncertainty:* {m_.get('uncertainty', 'unknown').capitalize()}")
        with c6:
            conf_val = "Low" if m_.get("uncertainty") == "high" or m_["valid_pct"] < 50 else ("Medium" if m_.get("uncertainty") == "medium" else "High")
            st.info(f"**Confidence in this screening:** {conf_val}  \n*Based on usable pixel coverage ({m_['valid_pct']}%) and baseline consistency.*")

        # ================================================================== #
        # Section B: What Does This Mean? (Farmer Guidance)
        # ================================================================== #
        st.subheader("Section B: What does this mean?")
        with st.expander("📖 Plain-language explanation & practical guidance", expanded=True):
            insights = generate_field_insights(m_, res.get("meta", {}))
            for ins in insights:
                st.markdown(f"- {ins}")

            st.markdown("---")
            st.markdown("**Recommended next steps for the field manager / farmer:**")
            st.markdown(
                "1. **Ground inspection:** Walk the flagged portions of the field to check for visible yellowing, standing water, pest attack, or soil differences.\n"
                "2. **Adjacent field comparison:** Compare with adjacent fields planted with the same crop at the same growth stage.\n"
                "3. **Check management records:** Consider recent fertilizer, irrigation, spray, or weeding dates that may influence greenness.\n"
                "4. **Re-screen if cloudy:** If usable satellite data was low, repeat this screening during a clearer calendar window (e.g. post-monsoon October–November)."
            )

        # ================================================================== #
        # Section D: Satellite Evidence & Visuals
        # ================================================================== #
        st.subheader("Section D: Satellite evidence & technical details")

        if os.path.exists(res.get("quicklook", "")):
            st.image(
                res["quicklook"],
                caption="Sentinel-2 Field Evidence: Typical healthy baseline (left) vs Observed period (center) vs Flagged stress locations (right).",
                width="stretch"
            )
            st.caption("Note: Light grey areas indicate clouds, shadows, or areas outside the field polygon.")

        col_dl1, col_dl2, col_dl3 = st.columns(3)
        if os.path.exists(res.get("pdf", "")):
            with open(res["pdf"], "rb") as fh:
                col_dl1.download_button(
                    f"⬇ Download field report PDF ({run['run_id']})",
                    fh.read(), file_name=os.path.basename(res["pdf"]),
                    mime="application/pdf", width="stretch"
                )
        if os.path.exists(res.get("geojson", "")):
            with open(res["geojson"], "rb") as fh:
                col_dl2.download_button(
                    "⬇ Download field boundary & metrics (GeoJSON)",
                    fh.read(), file_name=f"{run['run_id']}_field.geojson",
                    mime="application/geo+json", width="stretch"
                )
        if os.path.exists(res.get("metrics_json", "")):
            with open(res["metrics_json"], "rb") as fh:
                col_dl3.download_button(
                    "⬇ Download metrics summary (JSON)",
                    fh.read(), file_name=f"{run['run_id']}_metrics.json",
                    mime="application/json", width="stretch"
                )

        with st.expander("🔬 Technical details & provenance (for agronomists & specialists)"):
            ev_meta = res.get("meta", {}).get("event", {})
            st.markdown(f"""
            - **Observed NDVI mean:** `{m_.get('ndvi_mean')}`
            - **Historical Baseline NDVI mean:** `{m_.get('ndvi_mean', 0) - (m_.get('ndvi_delta') or 0):.3f}`
            - **ΔNDVI (Observed − Baseline):** `{m_.get('ndvi_delta')}`
            - **Standardized Anomaly (z-score):** `{m_.get('z_score')}`
            - **Normalized Difference Water Index (NDWI):** `{m_.get('ndwi_mean')}`
            - **Satellite sensor:** Sentinel-2 MSI L2A (`COPERNICUS/S2_SR_HARMONIZED`)
            - **Coordinate Reference System (CRS):** `{res.get('meta', {}).get('crs')}` @ 10m native pixel scale
            - **Observation window:** `{run['event_window'][0]} to {run['event_window'][1]}`
            - **Baseline years used:** `{run['baseline_years']}`
            """)
            if ev_meta.get("scenes"):
                st.markdown("**Sentinel-2 satellite acquisitions used in this assessment:**")
                scenes_rows = []
                for s in ev_meta["scenes"]:
                    scenes_rows.append({
                        "Acquisition Date": s.get("date", "?"),
                        "Study Area Clear %": f"{s.get('aoi_clear_pct', '?')}%" if s.get('aoi_clear_pct') is not None else "N/A",
                        "Whole Tile Cloud %": f"{s.get('tile_cloud_pct', s.get('cloud_pct', '?'))}%",
                        "Scene ID": s.get("id", "?")
                    })
                st.dataframe(pd.DataFrame(scenes_rows), hide_index=True, width="stretch")

# ========================================================================== #
# TAB 2: Saved Runs & Compare
# ========================================================================== #
with tab_compare:
    st.title("📁 Saved Field Screenings & Comparison")
    st.caption("All completed assessments are automatically stored. Review past screenings or compare across periods.")

    saved = list_saved_assessments()
    if not saved:
        st.info("No saved assessments found yet. Run an assessment in the first tab to begin.")
    else:
        st.subheader(f"{len(saved)} saved screening(s)")
        table_rows = []
        for rec in saved:
            m = rec.get("metrics", {})
            a_ha = m.get("area_ha", "?")
            a_ac = round(a_ha * HA_TO_ACRES, 2) if isinstance(a_ha, (int, float)) else "?"
            table_rows.append({
                "Run ID": rec["run_id"],
                "Mode": rec.get("mode", "?").upper(),
                "Field area": f"{a_ha} ha ({a_ac} ac)",
                "Stress %": f"{m.get('affected_pct', '?')}%",
                "Screening level": m.get("severity", "?").capitalize(),
                "Possible cause": m.get("cause", "?").capitalize(),
                "Usable data": f"{m.get('valid_pct', '?')}%",
                "Observation window": str(rec.get("event_window", "?")),
            })
        st.dataframe(pd.DataFrame(table_rows), hide_index=True, width="stretch")

        st.divider()
        st.subheader("Inspect a saved run")
        run_ids = [r["run_id"] for r in saved]
        chosen_id = st.selectbox("Select a previous run", ["(none)"] + run_ids)
        if chosen_id != "(none)":
            rec = load_saved_assessment(chosen_id)
            if rec:
                m = rec.get("metrics", {})
                a_ha_v = m.get("area_ha", 0)
                a_ac_v = round(a_ha_v * HA_TO_ACRES, 2) if isinstance(a_ha_v, (int, float)) else "?"
                aff_ha_v = m.get("affected_area_ha", 0)
                aff_ac_v = round(aff_ha_v * HA_TO_ACRES, 3) if isinstance(aff_ha_v, (int, float)) else "?"

                col1, col2, col3, col4 = st.columns(4)
                col1.metric("Field area", f"{a_ha_v} ha (~{a_ac_v} ac)")
                col2.metric("Usable satellite data", f"{m.get('valid_pct', '?')}%")
                col3.metric("Possible stress %", f"{m.get('affected_pct', '?')}%")
                col4.metric("Screening level", str(m.get("severity", "?")).capitalize())

                if rec.get("quicklook") and os.path.exists(rec["quicklook"]):
                    st.image(rec["quicklook"], caption=f"Satellite Evidence for {chosen_id}", width="stretch")
                if rec.get("pdf") and os.path.exists(rec["pdf"]):
                    with open(rec["pdf"], "rb") as fh:
                        st.download_button(
                            f"⬇ Download report PDF ({chosen_id})", fh.read(),
                            file_name=os.path.basename(rec["pdf"]), mime="application/pdf",
                            width="stretch"
                        )

        st.divider()
        st.subheader("Compare multiple assessments")
        if len(saved) < 2:
            st.info("Run at least two assessments to compare them.")
        else:
            compare_ids = st.multiselect("Select runs to compare (up to 6)", run_ids, max_selections=6)
            if len(compare_ids) >= 2:
                comp_rows = []
                warnings = []
                ref_win = None
                ref_mode = None
                for rid in compare_ids:
                    r_rec = load_saved_assessment(rid)
                    if not r_rec: continue
                    rm = r_rec.get("metrics", {})
                    comp_rows.append({
                        "Run ID": rid,
                        "Mode": r_rec.get("mode", "?").upper(),
                        "Field area (ha)": rm.get("area_ha", "?"),
                        "Stress %": rm.get("affected_pct", "?"),
                        "Flagged area (ha)": rm.get("affected_area_ha", "?"),
                        "Screening level": rm.get("severity", "?").capitalize(),
                        "Cause": rm.get("cause", "?").capitalize(),
                        "Usable data %": rm.get("valid_pct", "?"),
                        "NDVI Δ": rm.get("ndvi_delta", "?"),
                        "z-score": rm.get("z_score", "?"),
                        "Window": str(r_rec.get("event_window", "?")),
                    })
                    curr_win = str(r_rec.get("event_window"))
                    curr_mode = str(r_rec.get("mode"))
                    if ref_win is None:
                        ref_win, ref_mode = curr_win, curr_mode
                    else:
                        if curr_win != ref_win:
                            warnings.append(f"• Run {rid} has a different observation window ({curr_win}).")
                        if curr_mode != ref_mode:
                            warnings.append(f"• Run {rid} is in {curr_mode.upper()} mode, whereas other runs are {ref_mode.upper()}.")

                st.dataframe(pd.DataFrame(comp_rows), hide_index=True, width="stretch")
                if warnings:
                    st.warning("**Comparison caveats & warnings:**\n\n" + "\n".join(warnings) +
                               "\n\n*Different observation windows, weather patterns, or data modes make direct comparison less reliable.*")
                else:
                    st.success("✓ All compared runs share the same observation window and data mode.")