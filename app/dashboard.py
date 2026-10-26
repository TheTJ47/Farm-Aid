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
import math
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
import incident as incidentmod  # noqa: E402
from incident import discover_incident_scenes, fetch_incident_rasters, fetch_incident_demo  # noqa: E402
from report import generate_field_insights, HA_TO_ACRES  # noqa: E402
from runmeta import canonical_settings, new_run_id, param_key, \
    results_are_current  # noqa: E402
from storage import (DEFAULT_SAVED_DIR as SAVED_DIR,  # noqa: E402
                     list_saved_assessments, load_saved_assessment,
                     load_active_assessment, save_assessment)

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
ACTIVE_RUN_ID = "active_run_id"  # str: active run ID returned by assessment or loaded from saved
LAST_SUCCESS_MSG = "last_success_msg"  # str: success banner message synchronized with active run
DRAW = "draw_seq"    # counter to detect new drawings

for key, default in [(SEL, None), (RUN, None), (ACTIVE_RUN_ID, None), (LAST_SUCCESS_MSG, None), (DRAW, 0)]:
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
        st.session_state[ACTIVE_RUN_ID] = None
        st.session_state[LAST_SUCCESS_MSG] = None
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
st.sidebar.subheader("2. Assessment workflow & incident")

workflow_mode = st.sidebar.radio(
    "Assessment workflow",
    ["recent", "historical"],
    format_func=lambda w: "⚡ Recent damage / Assess now" if w == "recent"
    else "📅 Historical insurance claim",
    help="Recent damage searches for the latest available Sentinel-2 imagery. Historical claim lets you enter the reported incident date."
)

incident_date_input = None
if workflow_mode == "recent":
    st.sidebar.caption("Automatically retrieves latest Sentinel-2 passes over the selected boundary and compares with immediate pre-damage conditions.")
    search_window_days = st.sidebar.slider("Search window (days back)", min_value=15, max_value=90, value=45, step=5)
else:
    today_dt = dt.date.today()
    default_inc = dt.date(2025, 8, 15) if mode == "live" else dt.date(2025, 8, 15)
    incident_date_input = st.sidebar.date_input("Reported incident date", value=default_inc)
    search_window_days = st.sidebar.slider("Search window (± days from incident)", min_value=15, max_value=60, value=30, step=5)

claimed_peril = st.sidebar.selectbox(
    "Reported Peril / Damage Type",
    ["Inundation / Flood", "Drought / Dry Spell", "Other / Hail", "Excess Rainfall", "Unseasonal Rain", "General Crop Stress"]
)

max_cloud = st.sidebar.number_input(
    "Max tile cloud filter (%)",
    value=cfg.get("max_cloud_pct", 70),
    min_value=10, max_value=100, step=5,
    help="Rejects satellite scenes with higher cloud cover across the 100km tile."
)

# Optional Historical Baseline Years for supporting context
with st.sidebar.expander("⚙️ Advanced: Historical Multi-Year Norms", expanded=False):
    year_options = list(range(2020, 2027))
    years = st.multiselect(
        "Baseline comparison years (supporting context)",
        year_options,
        default=[y for y in cfg.get("baseline_years", [2022, 2023]) if y in year_options],
        help="Used as supporting multi-year context alongside the primary before-and-after observation comparison."
    )

st.sidebar.divider()
st.sidebar.subheader("3. Claim & policy info (optional)")
with st.sidebar.expander("📝 Claim & Insured Details", expanded=False):
    claim_ref = st.text_input("Claim Reference / ID", value="", placeholder="e.g. CLM-2026-9041")
    policy_no = st.text_input("Policy Number", value="", placeholder="e.g. POL-MH-458102")
    claimant_name = st.text_input("Claimant / Farmer Name", value="", placeholder="e.g. Ramesh Patil")
    crop_type = st.text_input("Insured Crop", value="", placeholder="e.g. Soybean / Cotton")

claim_info = {
    "claim_ref": claim_ref.strip() or "Not provided",
    "policy_no": policy_no.strip() or "Not provided",
    "claimant_name": claimant_name.strip() or "Not provided",
    "crop_type": crop_type.strip() or "Not specified",
    "claimed_cause": claimed_peril,
    "workflow_mode": workflow_mode,
    "incident_date": incident_date_input.isoformat() if incident_date_input else dt.date.today().isoformat(),
}

# Run button
st.sidebar.divider()
run_disabled = st.session_state[SEL] is None
if st.sidebar.button("🚀 Run before-and-after assessment",
                     type="primary", disabled=run_disabled):
    geom = st.session_state[SEL]["geom"]
    try:
        geom = validate_geometry(geom)
    except FarmAidDataError as e:
        st.session_state[RUN] = None
        st.session_state[ACTIVE_RUN_ID] = None
        st.session_state[LAST_SUCCESS_MSG] = None
        st.sidebar.error(str(e))
        geom = None

    if geom is not None:
        eff = copy.deepcopy(cfg)
        inc_str = incident_date_input.isoformat() if incident_date_input else dt.date.today().isoformat()
        eff["event_window"] = [
            (dt.date.fromisoformat(inc_str) - dt.timedelta(days=search_window_days)).isoformat(),
            (dt.date.fromisoformat(inc_str) + dt.timedelta(days=search_window_days)).isoformat()
        ]
        eff["baseline_years"] = sorted(years) if years else [2022, 2023]
        eff["max_cloud_pct"] = max_cloud

        current_key = param_key(mode, geom.wkt, [inc_str, workflow_mode, claimed_peril],
                                eff["baseline_years"],
                                canonical_settings(eff))
        run_id = new_run_id()
        procdir = os.path.join(ROOT, eff["data_dir"], "processed", run_id)
        os.makedirs(procdir, exist_ok=True)

        with st.spinner("Searching Sentinel-2 imagery and analyzing incident change…"):
            try:
                if mode == "live":
                    discovery = discover_incident_scenes(
                        eff, geom=geom, incident_date=inc_str, mode=workflow_mode,
                        search_window_days=search_window_days, max_cloud_pct=max_cloud
                    )
                    if discovery.get("data_sufficiency") == "insufficient":
                        st.session_state[RUN] = None
                        st.session_state[ACTIVE_RUN_ID] = None
                        st.session_state[LAST_SUCCESS_MSG] = None
                        st.sidebar.error(f"Assessment inconclusive — {discovery.get('notes')}")
                        geom = None
                    else:
                        meta = fetch_incident_rasters(eff, geom, discovery, procdir, peril=claimed_peril)
                else:
                    discovery, meta = fetch_incident_demo(
                        eff, geom, incident_date=inc_str, procdir=procdir, peril=claimed_peril
                    )

                if geom is not None:
                    grid_summary = run_grid(eff, procdir,
                                            os.path.join(ROOT, eff["out_dir"], run_id))
                    res = fieldmod.assess_field(
                        eff, geom, mode, root=ROOT, run_id=run_id,
                        rasters_dir=procdir, meta=meta, claim_info=claim_info)
                    run_data = {
                        "run_id": run_id,
                        "param_key": current_key,
                        "mode": mode,
                        "workflow_mode": workflow_mode,
                        "incident_date": inc_str,
                        "claimed_peril": claimed_peril,
                        "event_window": meta.get("event_window") or eff.get("event_window"),
                        "baseline_years": eff["baseline_years"],
                        "discovery": discovery,
                        "geom_wkt": geom.wkt,
                        "res": res,
                        "grid_summary": grid_summary,
                        "claim_info": claim_info,
                    }
                    save_assessment(run_data)
                    loaded = load_active_assessment(run_id)
                    if loaded is None:
                        st.session_state[RUN] = None
                        st.session_state[ACTIVE_RUN_ID] = None
                        st.session_state[LAST_SUCCESS_MSG] = None
                        st.sidebar.error(f"Failed to load assessment artifacts for {run_id}.")
                    else:
                        st.session_state[RUN] = loaded
                        st.session_state[ACTIVE_RUN_ID] = run_id
                        st.session_state[LAST_SUCCESS_MSG] = f"Incident assessment complete (Run ID: {run_id})."
                        st.rerun()
            except (FarmAidDataError, Exception) as e:  # noqa: BLE001
                st.session_state[RUN] = None
                st.session_state[ACTIVE_RUN_ID] = None
                st.session_state[LAST_SUCCESS_MSG] = None
                st.sidebar.error(f"Assessment failed — {e}")

if st.session_state.get(LAST_SUCCESS_MSG) and st.session_state.get(ACTIVE_RUN_ID):
    st.sidebar.success(st.session_state[LAST_SUCCESS_MSG])

if st.sidebar.button("🗑️ Clear all and reset"):
    st.session_state[SEL] = None
    st.session_state[RUN] = None
    st.session_state[ACTIVE_RUN_ID] = None
    st.session_state[LAST_SUCCESS_MSG] = None
    st.rerun()

# --------------------------------------------------------------------------- #
# Fingerprint & Staleness
# --------------------------------------------------------------------------- #
sel = st.session_state.get(SEL)
run = st.session_state.get(RUN)
active_run_id = st.session_state.get(ACTIVE_RUN_ID)

if active_run_id and run is None:
    loaded = load_active_assessment(active_run_id)
    if loaded is not None:
        run = loaded
        st.session_state[RUN] = loaded
    else:
        st.session_state[ACTIVE_RUN_ID] = None

current_key = None
if sel is not None:
    inc_str = incident_date_input.isoformat() if incident_date_input else dt.date.today().isoformat()
    eff_years = sorted(years) if years else [2022, 2023]
    eff_cfg = copy.deepcopy(cfg)
    eff_cfg["max_cloud_pct"] = max_cloud
    current_key = param_key(mode, sel["wkt"], [inc_str, workflow_mode, claimed_peril],
                            eff_years, canonical_settings(eff_cfg))
run_is_current = (run is not None and current_key is not None
                  and results_are_current(run.get("param_key"), current_key))

# --------------------------------------------------------------------------- #
# Main Tabs
# --------------------------------------------------------------------------- #
tab_assess, tab_compare = st.tabs(["📊 Field Assessment", "📁 Saved Runs & Compare"])

# ========================================================================== #
# TAB 1: Assessment
# ========================================================================== #
with tab_assess:
    st.title("🌾 FarmAid — Crop Damage Assessment System")
    badge_html = '<span class="badge-live">🔴 LIVE SATELLITE MODE (COPERNICUS SENTINEL-2 L2A)</span>' if mode == "live" else '<span class="badge-demo">🔵 DEMO SIMULATION MODE (OFFLINE SYNTHETIC)</span>'
    st.markdown(f"{badge_html} &nbsp; **Objective Satellite Damage Evidence for Crop Insurance Claim Adjudication**", unsafe_allow_html=True)
    st.caption("Standardized spectral screening for farmers, loss adjusters, and state researchers comparing claimed loss events against multi-year healthy baselines.")

    # 7-Step Workflow Guide
    with st.expander("ℹ️ How This Assessment System Works (7-Step Workflow)", expanded=False):
        st.markdown("""
        1. **Select Field:** Enter geographic coordinates & width in the sidebar, or draw the exact farm boundary on the interactive map.
        2. **Configure Dates:** Set the claimed observation period and historical baseline comparison years.
        3. **Enter Claim Details:** Optionally enter Claim ID, Policy Number, Insured Name, and Crop Type for report branding.
        4. **Review Pre-Run Summary:** Inspect the verified parcel geometry, area (ha/acres), and sensor parameters.
        5. **Run Assessment:** Sentinel-2 surface reflectance observations are masked for clouds/shadows and evaluated against the baseline.
        6. **Evaluate Findings:** Review affected percentage, estimated damage area, severity classification, and peril hypothesis.
        7. **Export Evidentiary Dossier:** Download the official PDF Evidence Report, GeoJSON boundary, or JSON audit record.
        """)

    if run is not None and not run_is_current:
        st.warning(
            f"⚠️ The results shown below belong to assessment run `{run['run_id']}`. "
            "Input parameters (dates, baseline, or boundary) have changed since this run was generated. "
            "Click 'Run before-and-after assessment' to process the new parameters."
        )

    # Pre-Assessment Review Card (always visible when field is selected)
    if sel is not None:
        ha_p = fieldmod.area_ha(sel["geom"])
        ac_p = round(ha_p * HA_TO_ACRES, 2)
        c_p = sel["geom"].centroid
        inc_disp = incident_date_input.isoformat() if incident_date_input else "Latest Available"
        wf_disp = "Recent Damage Assessment" if workflow_mode == "recent" else f"Historical Claim ({inc_disp})"

        st.markdown(f"""
        <div style="background-color: #f1f5f9; border-left: 4px solid #1e3a8a; padding: 10px 16px; border-radius: 4px; margin-bottom: 14px;">
            <div style="font-size: 0.82rem; font-weight: 700; color: #1e3a8a; text-transform: uppercase;">
                Pre-Assessment Incident Configuration
            </div>
            <div style="display: flex; flex-wrap: wrap; gap: 18px; margin-top: 6px; font-size: 0.88rem; color: #1e293b;">
                <div><b>Boundary Area:</b> {ha_p:.3f} ha (~{ac_p} acres)</div>
                <div><b>Centroid:</b> {c_p.y:.4f}°N, {c_p.x:.4f}°E</div>
                <div><b>Workflow:</b> {wf_disp}</div>
                <div><b>Reported Peril:</b> {claimed_peril}</div>
                <div><b>Search Window:</b> ±{search_window_days} days</div>
                <div><b>Mode:</b> {mode.upper()}</div>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.info("👋 **Welcome to FarmAid.** Specify your field boundary using coordinates in the sidebar (click 'Apply coordinates') or draw directly on the map below to begin.")

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

    # Layers 3 & 4 (Active run overlays)
    if run is not None and run.get("res"):
        res = run["res"]
        grid_geojson = os.path.join(res.get("outdir", ""), "damage.geojson")
        if os.path.exists(grid_geojson):
            fg_grid = folium.FeatureGroup(name="③ Regional screening grid (~300m cells)", show=False)
            try:
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
            except Exception as e:
                st.caption(f"ℹ️ Regional grid map layer omitted: {e}")

        fg_field = folium.FeatureGroup(name="④ Areas flagged for possible crop stress", show=True)
        if os.path.exists(res.get("geojson", "")):
            try:
                fgf = map_safe_geodataframe(gpd.read_file(res["geojson"]),
                                            drop_columns=("geometry_wkt", "param_key"))
                if not fgf.empty:
                    folium.GeoJson(
                        fgf,
                        style_function=lambda f: {"fillColor": "#C22D2D", "color": "#7F1D1D", "weight": 3, "fillOpacity": 0.25},
                        tooltip="Assessed Field Screening Result"
                    ).add_to(fg_field)
                    fg_field.add_to(m)
            except Exception as e:
                st.caption(f"ℹ️ Field boundary layer omitted: {e}")

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
        if input_mode == "Draw on map" and map_out and isinstance(map_out, dict):
            drawing = map_out.get("last_active_drawing")
            if drawing:
                g2 = drawing.get("geometry") if isinstance(drawing, dict) else None
                if isinstance(g2, str):
                    try: g2 = json.loads(g2)
                    except Exception: g2 = None
                if g2:
                    try:
                        new_geom = validate_geometry(to_shape(g2))
                        curr_geom = (st.session_state[SEL] or {}).get("geom")
                        if curr_geom is None or not curr_geom.equals(new_geom):
                            st.session_state[SEL] = {
                                "geom": new_geom, "wkt": new_geom.wkt, "source": "drawn",
                                "applied_coords": None
                            }
                            st.session_state[RUN] = None
                            st.session_state[ACTIVE_RUN_ID] = None
                            st.session_state[LAST_SUCCESS_MSG] = None
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
    # Results Section (Shown whenever an active run is loaded)
    # ---------------------------------------------------------------------- #
    if run is not None and run.get("res") is not None:
        res = run["res"]
        m_ = res.get("metrics", {})
        st.divider()

        # ================================================================== #
        # Section A: Executive Insurance Assessment Summary
        # ================================================================== #
        st.subheader("Section A: Executive assessment summary & findings")

        is_insufficient = m_.get("insufficient_data", False)
        is_no_evidence = m_.get("no_evidence_of_damage", False)
        valid_pct_v = m_.get("valid_pct", 0.0)
        sev_label = m_.get("severity", "unknown").capitalize()

        if is_insufficient:
            st.error(
                f"⚠️ **ASSESSMENT INCONCLUSIVE — INSUFFICIENT USABLE SATELLITE DATA**  \n"
                f"Only **{valid_pct_v}%** of the field boundary had clear observations after SCL cloud/shadow filtering "
                f"(below the 30% reliability threshold). Cloud contamination or shadows prevented conclusive assessment. "
                f"*This is a satellite data limitation, NOT evidence that the crop is undamaged.*"
            )
        elif is_no_evidence:
            st.success(
                f"✓ **ASSESSMENT COMPLETED — NO QUALIFYING VEGETATION DAMAGE DETECTED**  \n"
                f"Field greenness during the claimed observation window tracked within historical healthy norms "
                f"(0.0% qualifying negative anomaly across {valid_pct_v}% clear observations)."
            )
        else:
            st.warning(
                f"⚠️ **ASSESSMENT COMPLETED — {sev_label.upper()} DAMAGE INDICATORS FLAGGED**  \n"
                f"An estimated **{m_['affected_pct']}%** of the assessable field area (~{m_.get('affected_area_ha', 0)} ha) "
                f"exhibits statistically significant vegetation depression (z < -1.5) relative to multi-year healthy baselines."
            )

        area_ha_v = m_["area_ha"]
        area_ac_v = round(area_ha_v * HA_TO_ACRES, 2)
        aff_ha_v = m_.get("affected_area_ha", 0)
        aff_ac_v = round(aff_ha_v * HA_TO_ACRES, 3)

        # Incident Satellite Observation Timeline Panel
        disc = run.get("discovery")
        if disc:
            with st.container():
                st.markdown("##### 🛰️ Incident Satellite Observation Timeline & Evidence Matching")
                sel_pre = disc.get("selected_pre")
                sel_post = disc.get("selected_post")

                t_cols = st.columns(3)
                with t_cols[0]:
                    if sel_pre:
                        st.markdown(f"""
                        <div style="background-color: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 6px; padding: 10px 14px;">
                            <div style="font-size: 0.75rem; font-weight: 700; color: #065f46; text-transform: uppercase;">Pre-Incident Observation</div>
                            <div style="font-size: 1.2rem; font-weight: 700; color: #064e3b; margin-top: 2px;">{sel_pre.get('date')}</div>
                            <div style="font-size: 0.8rem; color: #047857; margin-top: 2px;">
                                {abs(sel_pre.get('days_from_incident', 0))} days before incident<br/>
                                <b>Field Clear:</b> {sel_pre.get('field_clear_pct', '?')}% | <b>Cloud:</b> {sel_pre.get('tile_cloud_pct', '?')}%
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                    else:
                        st.warning("No pre-incident observation found.")

                with t_cols[1]:
                    inc_label = run.get("incident_date", "N/A")
                    st.markdown(f"""
                    <div style="background-color: #fffbeb; border: 1px solid #fde68a; border-radius: 6px; padding: 10px 14px; text-align: center;">
                        <div style="font-size: 0.75rem; font-weight: 700; color: #92400e; text-transform: uppercase;">Claimed Incident Date</div>
                        <div style="font-size: 1.2rem; font-weight: 700; color: #78350f; margin-top: 2px;">{inc_label}</div>
                        <div style="font-size: 0.8rem; color: #b45309; margin-top: 2px;">
                            <b>Peril:</b> {run.get('claimed_peril', 'Claimed Peril')}<br/>
                            <b>Workflow:</b> {run.get('workflow_mode', 'historical').capitalize()}
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                with t_cols[2]:
                    if sel_post:
                        st.markdown(f"""
                        <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 10px 14px;">
                            <div style="font-size: 0.75rem; font-weight: 700; color: #1e40af; text-transform: uppercase;">Post-Incident Observation</div>
                            <div style="font-size: 1.2rem; font-weight: 700; color: #1e3a8a; margin-top: 2px;">{sel_post.get('date')}</div>
                            <div style="font-size: 0.8rem; color: #2563eb; margin-top: 2px;">
                                {sel_post.get('days_from_incident', 0)} days after incident<br/>
                                <b>Field Clear:</b> {sel_post.get('field_clear_pct', '?')}% | <b>Cloud:</b> {sel_post.get('tile_cloud_pct', '?')}%
                            </div>
                        </div>
                        """, unsafe_allow_html=True)
                    else:
                        st.warning("No post-incident observation found.")

                st.caption(f"ℹ️ {disc.get('notes', '')}")
                st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

        # 4 Core KPI Cards with explicit insurance terminology
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Field Area (Total)</div>
                <div class="metric-value">{area_ha_v} ha</div>
                <div class="metric-sub">~{area_ac_v} acres inside boundary</div>
            </div>
            """, unsafe_allow_html=True)
        with c2:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Usable Data Coverage</div>
                <div class="metric-value">{valid_pct_v}%</div>
                <div class="metric-sub">{m_.get('n_valid_pixels', '?')} / {m_.get('n_field_pixels', '?')} clear pixels analyzed</div>
            </div>
            """, unsafe_allow_html=True)
        with c3:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Estimated Affected Area</div>
                <div class="metric-value">{aff_ha_v} ha</div>
                <div class="metric-sub">~{aff_ac_v} acres ({m_['affected_pct']}% of usable px)</div>
            </div>
            """, unsafe_allow_html=True)
        with c4:
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">Damage Severity Tier</div>
                <div class="metric-value">{sev_label}</div>
                <div class="metric-sub">Threshold: None &lt;5%, Low 5-15%, Mod 15-35%, High &ge;35%</div>
            </div>
            """, unsafe_allow_html=True)

        c5, c6 = st.columns(2)
        with c5:
            cause_label = m_["cause"].capitalize()
            if m_["cause"] == "unknown":
                cause_text = "Unknown — Spectral evidence alone cannot reliably distinguish cause"
            elif m_["cause"] == "flood":
                cause_text = "Inundation / Waterlogging (Elevated NDWI moisture signal > 0.05)"
            elif m_["cause"] == "drought":
                cause_text = "Moisture Deficit / Drought (Persistent vegetative drop without surface moisture)"
            else:
                cause_text = cause_label
            st.info(f"**Likely Peril / Cause Hypothesis:** {cause_text}  \n*Spectral indicator uncertainty:* {m_.get('uncertainty', 'unknown').capitalize()}")
        with c6:
            conf_val = "Low / Inconclusive" if is_insufficient or m_.get("uncertainty") == "high" or valid_pct_v < 50 else ("Moderate" if m_.get("uncertainty") == "medium" else "Supported")
            st.info(f"**Evidence Reliability Status:** {conf_val}  \n*Based on clear pixel coverage ({valid_pct_v}%), baseline consistency, and cloud mask checks.*")

        # ================================================================== #
        # Section B: Guidance & Terminology Explanations
        # ================================================================== #
        st.subheader("Section B: Claim interpretation & technical definitions")
        with st.expander("📖 Understanding This Assessment — Metric Definitions & Plain-Language Guide", expanded=False):
            st.markdown("""
            **Technical Metrics Demystified for Farmers & Claim Assessors:**
            - **NDVI (Normalized Difference Vegetation Index):** A satellite indicator of live green vegetation based on Red and Near-Infrared reflectance. Ranges from -1 to +1. Changes reflect canopy vigour, but do NOT directly measure harvest yield or prove crop loss on their own.
            - **Baseline NDVI:** The multi-year average greenness of this exact field during the identical calendar window in historical healthy years.
            - **NDVI Change (ΔNDVI = Observed − Baseline):** Negative values mean the canopy is less green than the historical reference. Small fluctuations (within ±0.05) are normal seasonal variation; larger drops indicate vegetative stress.
            - **Z-Score (Standardized Anomaly):** Measures how many standard deviations the observed NDVI is from the historical baseline. A score of *z < -1.5* means the vegetative drop is statistically abnormal relative to historical variations.
            - **Usable Data Coverage:** The percentage of field pixels remaining after cloud, dense shadow, water, and snow masking. It is **not** the crop health percentage. Coverage below 30% renders an assessment inconclusive.
            - **Estimated Affected Area & Share:** The percentage of *usable, clear* field pixels meeting the anomaly threshold (*z < -1.5* AND *Observed < Baseline*), multiplied by the field area.
            - **Damage Severity Classification:** Standardized categorization: None (<5%), Low (5–15%), Moderate (15–35%), or High (≥35% of clear field pixels flagged).
            - **Likely Cause:** A spectral hypothesis based on moisture (NDWI) and dryness patterns. Where satellite data cannot distinguish causes (e.g. hail defoliation vs pests vs fertilizer timing), the system reports **Unknown**.
            """)

        with st.expander("📋 Loss Adjuster Verification Checklist & Next Steps", expanded=True):
            insights = generate_field_insights(m_, res.get("meta", {}))
            st.markdown("**Evidence-Based Observations:**")
            for ins in insights:
                st.markdown(f"- {ins}")

            st.markdown("---")
            st.markdown("**Field Verification Checklist for Claim Settlement:**")
            st.markdown(
                "1. **On-Site Field Walk:** Inspect flagged sections of the parcel for physical crop lodging, pest infestation, flood silt lines, or soil erosion.\n"
                "2. **Adjacent Parcel Cross-Check:** Verify whether neighboring plots with identical crop types exhibited similar vegetative decline during the same period.\n"
                "3. **Agronomic Timeline Audit:** Compare sowing dates, fertilizer application logs, and irrigation schedules against the satellite observation window.\n"
                "4. **Local Meteorological Data:** Cross-reference local automatic weather station (AWS) rainfall or temperature records with the claimed peril date."
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
            b_mean_val = m_.get("baseline_ndvi_mean")
            if b_mean_val is None and m_.get("ndvi_mean") is not None and m_.get("ndvi_delta") is not None:
                b_mean_val = round(m_["ndvi_mean"] - m_["ndvi_delta"], 3)

            def _fmt_stat(val, decimals=3):
                if val is None or not isinstance(val, (int, float)) or not math.isfinite(val):
                    return "N/A (insufficient clear data)"
                return f"{val:.{decimals}f}"

            is_incident = res.get("meta", {}).get("workflow") == "incident_comparison"
            obs_label = "Post-Incident NDVI mean" if is_incident else "Observed NDVI mean"
            base_label = "Pre-Incident NDVI mean" if is_incident else "Historical Baseline NDVI mean"
            delta_label = "Incident Change (ΔNDVI)" if is_incident else "ΔNDVI (Observed − Baseline)"

            st.markdown(f"""
            - **{obs_label}:** `{_fmt_stat(m_.get('ndvi_mean'))}`
            - **{base_label}:** `{_fmt_stat(b_mean_val)}`
            - **{delta_label}:** `{_fmt_stat(m_.get('ndvi_delta'))}`
            - **Standardized Anomaly (z-score):** `{_fmt_stat(m_.get('z_score'), 2)}`
            - **Normalized Difference Water Index (NDWI):** `{_fmt_stat(m_.get('ndwi_mean'))}`
            - **Satellite sensor:** Sentinel-2 MSI L2A (`COPERNICUS/S2_SR_HARMONIZED`)
            - **Coordinate Reference System (CRS):** `{res.get('meta', {}).get('crs')}` @ 10m native pixel scale
            - **Assessment workflow:** `{'Incident Before-and-After Comparison' if is_incident else 'Multi-Year Baseline Screening'}`
            """)

            disc_obj = run.get("discovery")
            if disc_obj and (disc_obj.get("pre_scenes") or disc_obj.get("post_scenes")):
                st.markdown("**Sentinel-2 Satellite Imagery Candidates & Suitability Audit:**")
                cand_rows = []
                for s in disc_obj.get("pre_scenes", [])[:4]:
                    cand_rows.append({
                        "Timing": "Pre-Incident Candidate",
                        "Acquisition Date": s.get("date", "?"),
                        "Days from Incident": f"{s.get('days_from_incident', '?')}d",
                        "Field Clear %": f"{s.get('field_clear_pct', '?')}%",
                        "Tile Cloud %": f"{s.get('tile_cloud_pct', '?')}%",
                        "Selected": "✓ YES (Reference)" if (disc_obj.get("selected_pre") or {}).get("id") == s.get("id") else "No",
                        "Status": "Suitable" if s.get("is_suitable") else "Cloud/Shadow Limited"
                    })
                for s in disc_obj.get("post_scenes", [])[:4]:
                    cand_rows.append({
                        "Timing": "Post-Incident Candidate",
                        "Acquisition Date": s.get("date", "?"),
                        "Days from Incident": f"+{s.get('days_from_incident', '?')}d",
                        "Field Clear %": f"{s.get('field_clear_pct', '?')}%",
                        "Tile Cloud %": f"{s.get('tile_cloud_pct', '?')}%",
                        "Selected": "✓ YES (Impact)" if (disc_obj.get("selected_post") or {}).get("id") == s.get("id") else "No",
                        "Status": "Suitable" if s.get("is_suitable") else "Cloud/Shadow Limited"
                    })
                st.dataframe(pd.DataFrame(cand_rows), hide_index=True, width="stretch")
            elif ev_meta.get("scenes"):
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
            win = rec.get("event_window")
            if isinstance(win, (list, tuple)) and len(win) == 2:
                win_disp = f"{win[0]} → {win[1]}"
            else:
                win_disp = str(win or "N/A")
            table_rows.append({
                "Run ID": rec["run_id"],
                "Mode": rec.get("mode", "?").upper(),
                "Field area": f"{a_ha} ha ({a_ac} ac)",
                "Stress %": f"{m.get('affected_pct', '?')}%",
                "Screening level": m.get("severity", "?").capitalize(),
                "Possible cause": m.get("cause", "?").capitalize(),
                "Usable data": f"{m.get('valid_pct', '?')}%",
                "Observation window": win_disp,
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
                c_dl, c_load = st.columns(2)
                if rec.get("pdf") and os.path.exists(rec["pdf"]):
                    with open(rec["pdf"], "rb") as fh:
                        c_dl.download_button(
                            f"⬇ Download report PDF ({chosen_id})", fh.read(),
                            file_name=os.path.basename(rec["pdf"]), mime="application/pdf",
                            width="stretch"
                        )
                if c_load.button("🔄 Reopen this assessment in active view", width="stretch"):
                    loaded_active = load_active_assessment(chosen_id)
                    if loaded_active is None:
                        st.error(f"Failed to load assessment {chosen_id}.")
                    else:
                        import shapely.wkt
                        geom_saved = shapely.wkt.loads(loaded_active["geom_wkt"])
                        st.session_state[SEL] = {
                            "geom": geom_saved,
                            "wkt": loaded_active["geom_wkt"],
                            "source": "reopened_saved",
                            "applied_coords": None
                        }
                        st.session_state[RUN] = loaded_active
                        st.session_state[ACTIVE_RUN_ID] = chosen_id
                        st.session_state[LAST_SUCCESS_MSG] = f"Loaded saved assessment (Run ID: {chosen_id})."
                        st.success(f"Assessment {chosen_id} loaded into active view! Switch to Tab 1 to inspect.")
                        st.rerun()

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
                    cw = r_rec.get("event_window")
                    cw_disp = f"{cw[0]} → {cw[1]}" if isinstance(cw, (list, tuple)) and len(cw) == 2 else str(cw or "N/A")
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
                        "Window": cw_disp,
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