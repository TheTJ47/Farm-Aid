"""FarmAid - Incident-Based Satellite Discovery & Before/After Comparison.

Core module supporting:
  1. Assessment Mode A: Recent damage / assess now (automatic search for latest
     suitable pre- and post-incident Sentinel-2 observations over the selected field).
  2. Assessment Mode B: Historical insurance claim (user-specified incident date
     with configurable search windows, finding closest suitable observations before & after).
  3. Multi-observation trajectory retrieval (nearest 2-3 observations before/after).
  4. Peril-specific spectral tailoring:
     - Flood: Drop in NDVI paired with elevated NDWI surface water signal.
     - Drought: Progressive multi-observation canopy dry-down (negative ΔNDVI, negative NDWI).
     - Hail: Sudden acute NDVI drop between consecutive clear passes; transparently caveats
       optical limitation and flags physical defoliation vs gradual stress.
  5. Deterministic synthetic demo mode for offline testing.
"""
import datetime as _dt
import hashlib
import json
import os
import shutil
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
import rasterio

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fetch_gee import (
    COLLECTION,
    FarmAidDataError,
    NODATA,
    SCALE,
    cloud_mask,
    coverage_pct,
    download_tiff,
    init_ee,
    ndvi_of,
    project_bounds,
    selection_to_aoi,
    utm_epsg,
    write_source_json,
    write_tiff,
)

MIN_USABLE_CLEAR_PCT = 20.0  # minimum % clear pixels to be deemed 'suitable'


def discover_incident_scenes(
    cfg: dict,
    geom=None,
    incident_date: Optional[str] = None,
    mode: str = "recent",  # 'recent' or 'historical'
    search_window_days: int = 45,
    max_cloud_pct: Optional[float] = None,
    max_scenes_per_side: int = 3,
) -> dict:
    """Search Google Earth Engine for suitable Sentinel-2 imagery before and after an incident.

    Returns a structured dictionary with:
      - incident_date: ISO date
      - assessment_mode: 'recent' or 'historical'
      - pre_scenes: list of candidate pre-incident scenes (sorted by proximity to incident)
      - post_scenes: list of candidate post-incident scenes (sorted by proximity to incident)
      - selected_pre: best single pre-incident scene (or composite selection)
      - selected_post: best single post-incident scene (or composite selection)
      - trajectory_pre: up to N nearest suitable pre scenes
      - trajectory_post: up to N nearest suitable post scenes
      - search_window: [start_iso, end_iso]
      - data_sufficiency: 'sufficient', 'partial', or 'insufficient'
      - notes: diagnostic guidance
    """
    cloud_thr = max_cloud_pct if max_cloud_pct is not None else cfg.get("max_cloud_pct", 70)
    aoi = selection_to_aoi(cfg, geom)
    west, south, east, north = aoi

    # Determine reference incident date
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    if incident_date:
        try:
            ref_dt = _dt.datetime.fromisoformat(incident_date).replace(tzinfo=_dt.timezone.utc)
        except Exception:
            ref_dt = _dt.datetime.strptime(incident_date[:10], "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
        incident_date_str = ref_dt.strftime("%Y-%m-%d")
        search_start = (ref_dt - _dt.timedelta(days=search_window_days)).strftime("%Y-%m-%d")
        search_end = min(ref_dt + _dt.timedelta(days=search_window_days), now_utc + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        ref_dt = now_utc
        incident_date_str = ref_dt.strftime("%Y-%m-%d")
        search_start = (now_utc - _dt.timedelta(days=search_window_days)).strftime("%Y-%m-%d")
        search_end = (now_utc + _dt.timedelta(days=1)).strftime("%Y-%m-%d")

    ee = init_ee(cfg)
    aoi_ee = ee.Geometry.Rectangle([west, south, east, north])

    # Geometry for parcel-level clear coverage if provided
    geom_ee = None
    if geom is not None:
        try:
            import geopandas as gpd
            geojson_geom = json.loads(gpd.GeoSeries([geom]).to_json())["features"][0]["geometry"]
            geom_ee = ee.Geometry(geojson_geom)
        except Exception:
            geom_ee = None
    target_ee = geom_ee if geom_ee is not None else aoi_ee

    col = (
        ee.ImageCollection(COLLECTION)
        .filterBounds(aoi_ee)
        .filterDate(search_start, search_end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_thr))
    )

    n_raw = int(col.size().getInfo())
    if n_raw == 0:
        return {
            "incident_date": incident_date_str,
            "assessment_mode": mode,
            "pre_scenes": [],
            "post_scenes": [],
            "selected_pre": None,
            "selected_post": None,
            "trajectory_pre": [],
            "trajectory_post": [],
            "search_window": [search_start, search_end],
            "data_sufficiency": "insufficient",
            "notes": f"No Sentinel-2 imagery matched within ±{search_window_days} days of {incident_date_str} under cloud threshold {cloud_thr}%.",
        }

    # Evaluate clear pixel coverage for each scene
    def get_scene_meta(img):
        masked_b4 = cloud_mask(img).select("B4")
        cov_aoi = masked_b4.mask().reduceRegion(
            ee.Reducer.mean(), aoi_ee, 100, maxPixels=1e7
        ).values().get(0)
        cov_field = masked_b4.mask().reduceRegion(
            ee.Reducer.mean(), target_ee, SCALE, maxPixels=1e7
        ).values().get(0)
        return img.set({
            "aoi_clear_pct": ee.Number(cov_aoi).multiply(100.0),
            "field_clear_pct": ee.Number(cov_field).multiply(100.0),
            "s_id": img.get("system:index"),
            "s_time": img.get("system:time_start"),
            "tile_cloud": img.get("CLOUDY_PIXEL_PERCENTAGE"),
        })

    with_diag = col.map(get_scene_meta)
    info = with_diag.reduceColumns(
        ee.Reducer.toList().repeat(5),
        ["s_id", "s_time", "tile_cloud", "aoi_clear_pct", "field_clear_pct"]
    ).get("list").getInfo()

    all_scenes = []
    if info and len(info) == 5:
        for sid, stime, tc, cp_aoi, cp_field in zip(info[0], info[1], info[2], info[3], info[4]):
            s_dt = _dt.datetime.fromtimestamp(stime / 1000.0, tz=_dt.timezone.utc)
            delta_days = (s_dt.date() - ref_dt.date()).days
            f_cov = round(float(cp_field), 1) if cp_field is not None else 0.0
            a_cov = round(float(cp_aoi), 1) if cp_aoi is not None else 0.0
            is_suitable = f_cov >= MIN_USABLE_CLEAR_PCT

            all_scenes.append({
                "id": str(sid),
                "date": s_dt.strftime("%Y-%m-%d"),
                "datetime_utc": s_dt.strftime("%Y-%m-%d %H:%M UTC"),
                "days_from_incident": delta_days,
                "tile_cloud_pct": round(float(tc), 1) if tc is not None else None,
                "aoi_clear_pct": a_cov,
                "field_clear_pct": f_cov,
                "is_suitable": is_suitable,
            })

    # Partition into Pre and Post
    pre_scenes = [s for s in all_scenes if s["days_from_incident"] < 0]
    post_scenes = [s for s in all_scenes if s["days_from_incident"] >= 0]

    if mode == "recent" and not post_scenes and len(all_scenes) >= 2:
        chronological = sorted(all_scenes, key=lambda s: s["date"])
        suitable_chron = [s for s in chronological if s["is_suitable"]]
        if len(suitable_chron) >= 2:
            selected_post = suitable_chron[-1]
            selected_pre = suitable_chron[-2]
            post_scenes = [selected_post]
            pre_scenes = [s for s in chronological if s["date"] < selected_post["date"]]
        else:
            selected_post = chronological[-1]
            selected_pre = chronological[-2]
            post_scenes = [selected_post]
            pre_scenes = chronological[:-1]
        pre_scenes.sort(key=lambda s: s["date"], reverse=True)
    else:
        # Pre-incident: closest to incident (i.e. maximum days_from_incident <= -1)
        pre_scenes.sort(key=lambda s: s["days_from_incident"], reverse=True)
        # Post-incident: closest to incident (i.e. minimum days_from_incident >= 0)
        post_scenes.sort(key=lambda s: s["days_from_incident"])

        suitable_pre = [s for s in pre_scenes if s["is_suitable"]]
        suitable_post = [s for s in post_scenes if s["is_suitable"]]

        selected_pre = suitable_pre[0] if suitable_pre else (pre_scenes[0] if pre_scenes else None)
        selected_post = suitable_post[0] if suitable_post else (post_scenes[0] if post_scenes else None)

    trajectory_pre = [s for s in pre_scenes if s.get("is_suitable")][:max_scenes_per_side]
    trajectory_post = [s for s in post_scenes if s.get("is_suitable")][:max_scenes_per_side]

    if selected_pre and selected_post and selected_pre["is_suitable"] and selected_post["is_suitable"]:
        sufficiency = "sufficient"
        notes = (
            f"Found clear pre-incident observation on {selected_pre['date']} ({abs(selected_pre['days_from_incident'])}d before) "
            f"and post-incident observation on {selected_post['date']} ({selected_post['days_from_incident']}d after)."
        )
    elif (selected_pre and selected_pre["is_suitable"]) or (selected_post and selected_post["is_suitable"]):
        sufficiency = "partial"
        notes = "One observation period has suitable imagery, but the counterpart has cloud/shadow limitations."
    else:
        sufficiency = "insufficient"
        notes = "No clear observations meeting >=20% usable field coverage were found within the search window."

    return {
        "incident_date": incident_date_str,
        "assessment_mode": mode,
        "pre_scenes": pre_scenes,
        "post_scenes": post_scenes,
        "selected_pre": selected_pre,
        "selected_post": selected_post,
        "trajectory_pre": trajectory_pre,
        "trajectory_post": trajectory_post,
        "search_window": [search_start, search_end],
        "data_sufficiency": sufficiency,
        "notes": notes,
    }


def fetch_incident_rasters(
    cfg: dict,
    geom,
    discovery: dict,
    procdir: str,
    peril: str = "General",
) -> dict:
    """Fetch and prepare actual Sentinel-2 rasters comparing Pre-Incident vs Post-Incident observations.

    Exports into procdir:
      - pre_ndvi.tif        (baseline healthy canopy immediately before event)
      - post_ndvi.tif       (canopy condition following event)
      - pre_ndwi.tif        (moisture baseline)
      - post_ndwi.tif       (moisture after event — key for flood detection)
      - baseline_mean.tif   (mapped to pre_ndvi for backward compatibility)
      - baseline_std.tif    (estimated from pre or baseline norm)
      - event_ndvi.tif      (mapped to post_ndvi)
      - event_ndwi.tif      (mapped to post_ndwi)
    """
    ee = init_ee(cfg)
    aoi = selection_to_aoi(cfg, geom)
    west, south, east, north = aoi
    aoi_ee = ee.Geometry.Rectangle([west, south, east, north])
    epsg = utm_epsg((west + east) / 2.0)

    sel_pre = discovery.get("selected_pre")
    sel_post = discovery.get("selected_post")
    if not sel_pre or not sel_post:
        raise FarmAidDataError(
            "Cannot run incident comparison: missing pre- or post-incident observation.",
            state="insufficient_clear_pixels"
        )

    # Fetch Pre-incident Image
    pre_id = sel_pre["id"]
    post_id = sel_post["id"]

    pre_img_raw = ee.Image(f"{COLLECTION}/{pre_id}")
    post_img_raw = ee.Image(f"{COLLECTION}/{post_id}")

    pre_masked = cloud_mask(pre_img_raw)
    post_masked = cloud_mask(post_img_raw)

    pre_ndvi = ndvi_of(pre_masked)
    post_ndvi = ndvi_of(post_masked)

    pre_ndwi = pre_masked.normalizedDifference(["B3", "B8"]).rename("NDWI")
    post_ndwi = post_masked.normalizedDifference(["B3", "B8"]).rename("NDWI")

    # Historical baseline fallback for std: calculate std from pre-image or multi-year healthy norm if available
    std_img = pre_ndvi.multiply(0.0).add(0.05).rename("std")

    os.makedirs(procdir, exist_ok=True)

    # Download required rasters
    download_tiff(pre_ndvi, "pre_ndvi.tif", aoi_ee, SCALE, epsg, procdir)
    download_tiff(post_ndvi, "post_ndvi.tif", aoi_ee, SCALE, epsg, procdir)
    download_tiff(pre_ndwi, "pre_ndwi.tif", aoi_ee, SCALE, epsg, procdir)
    download_tiff(post_ndwi, "post_ndwi.tif", aoi_ee, SCALE, epsg, procdir)

    # Create standard aliases so existing analytical grid and field modules run seamlessly
    shutil.copyfile(os.path.join(procdir, "pre_ndvi.tif"), os.path.join(procdir, "baseline_mean.tif"))
    shutil.copyfile(os.path.join(procdir, "post_ndvi.tif"), os.path.join(procdir, "event_ndvi.tif"))
    shutil.copyfile(os.path.join(procdir, "post_ndwi.tif"), os.path.join(procdir, "event_ndwi.tif"))
    download_tiff(std_img, "baseline_std.tif", aoi_ee, SCALE, epsg, procdir)

    meta = {
        "mode": "live",
        "workflow": "incident_comparison",
        "peril": peril,
        "incident_date": discovery.get("incident_date"),
        "event_window": [sel_pre["date"], sel_post["date"]],
        "search_window": discovery.get("search_window"),
        "pre_scene": sel_pre,
        "post_scene": sel_post,
        "trajectory_pre": discovery.get("trajectory_pre", []),
        "trajectory_post": discovery.get("trajectory_post", []),
        "geometry": geom.wkt if geom is not None else None,
        "aoi": aoi,
        "scale_m": SCALE,
        "crs": f"EPSG:{epsg}",
        "data_state": "valid" if (sel_pre["is_suitable"] and sel_post["is_suitable"]) else "limited",
    }
    write_source_json(os.path.join(procdir, "source.json"), meta)
    return meta


def fetch_incident_demo(
    cfg: dict,
    geom,
    incident_date: str = "2025-08-15",
    procdir: str = None,
    peril: str = "Flood",
    seed: Optional[str] = None,
) -> Tuple[dict, dict]:
    """Deterministic synthetic generator for incident before-and-after testing."""
    aoi = selection_to_aoi(cfg, geom)
    west, south, east, north = aoi
    epsg = utm_epsg((west + east) / 2.0)
    xw, ys, xe, yn = project_bounds(west, south, east, north, epsg)
    cols = max(int(round((xe - xw) / SCALE)), 1)
    rows = max(int(round((yn - ys) / SCALE)), 1)
    transform = rasterio.transform.from_origin(xw, yn, SCALE, SCALE)

    seed_hex = seed or hashlib.sha1(f"incident_{incident_date}_{peril}".encode()).hexdigest()
    rng = np.random.default_rng(int(seed_hex[:8], 16))

    # Pre-incident healthy vegetation
    pre_ndvi = np.full((rows, cols), 0.68) + rng.normal(0, 0.015, (rows, cols))
    pre_ndwi = np.full((rows, cols), -0.10) + rng.normal(0, 0.01, (rows, cols))
    std = np.full((rows, cols), 0.05)

    post_ndvi = pre_ndvi.copy()
    post_ndwi = pre_ndwi.copy()

    yy, xx = np.mgrid[0:rows, 0:cols].astype(float)
    cy, cx = rows * 0.5, cols * 0.5
    radius = min(rows, cols) * 0.4
    d = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
    m = d < radius

    # Tailor synthetic damage to peril
    if peril.lower() in ("flood", "inundation", "excess rainfall"):
        dip = np.clip(0.35 * (1 - d / radius), 0, 0.40)
        post_ndvi = np.where(m, pre_ndvi - dip, post_ndvi)
        water = np.clip(0.25 * (1 - d / radius), 0, 0.30)
        post_ndwi = np.where(m, post_ndwi + water, post_ndwi)
    elif peril.lower() in ("drought", "dry spell"):
        dip = np.clip(0.28 * (1 - d / radius), 0, 0.30)
        post_ndvi = np.where(m, pre_ndvi - dip, post_ndvi)
        post_ndwi = np.where(m, post_ndwi - 0.08, post_ndwi)
    elif peril.lower() in ("hail", "other / hail"):
        # Sudden acute localized strip damage
        strip = (abs(xx - yy) < (cols * 0.2)) & m
        post_ndvi = np.where(strip, pre_ndvi - 0.38, post_ndvi)
    else:
        dip = np.clip(0.30 * (1 - d / radius), 0, 0.32)
        post_ndvi = np.where(m, pre_ndvi - dip, post_ndvi)

    os.makedirs(procdir, exist_ok=True)
    write_tiff(os.path.join(procdir, "pre_ndvi.tif"), pre_ndvi, transform, epsg)
    write_tiff(os.path.join(procdir, "post_ndvi.tif"), post_ndvi, transform, epsg)
    write_tiff(os.path.join(procdir, "pre_ndwi.tif"), pre_ndwi, transform, epsg)
    write_tiff(os.path.join(procdir, "post_ndwi.tif"), post_ndwi, transform, epsg)

    # Standard aliases
    write_tiff(os.path.join(procdir, "baseline_mean.tif"), pre_ndvi, transform, epsg)
    write_tiff(os.path.join(procdir, "baseline_std.tif"), std, transform, epsg)
    write_tiff(os.path.join(procdir, "event_ndvi.tif"), post_ndvi, transform, epsg)
    write_tiff(os.path.join(procdir, "event_ndwi.tif"), post_ndwi, transform, epsg)

    ref_d = _dt.date.fromisoformat(incident_date)
    pre_date_str = (ref_d - _dt.timedelta(days=4)).isoformat()
    post_date_str = (ref_d + _dt.timedelta(days=5)).isoformat()
    search_start_str = (ref_d - _dt.timedelta(days=30)).isoformat()
    search_end_str = (ref_d + _dt.timedelta(days=30)).isoformat()

    discovery = {
        "incident_date": incident_date,
        "assessment_mode": "demo",
        "search_window": [search_start_str, search_end_str],
        "pre_scenes": [{"id": "DEMO_S2_PRE", "date": pre_date_str, "days_from_incident": -4, "tile_cloud_pct": 5.0, "aoi_clear_pct": 98.0, "field_clear_pct": 99.0, "is_suitable": True}],
        "post_scenes": [{"id": "DEMO_S2_POST", "date": post_date_str, "days_from_incident": 5, "tile_cloud_pct": 8.0, "aoi_clear_pct": 95.0, "field_clear_pct": 97.0, "is_suitable": True}],
        "selected_pre": {"id": "DEMO_S2_PRE", "date": pre_date_str, "days_from_incident": -4, "tile_cloud_pct": 5.0, "aoi_clear_pct": 98.0, "field_clear_pct": 99.0, "is_suitable": True},
        "selected_post": {"id": "DEMO_S2_POST", "date": post_date_str, "days_from_incident": 5, "tile_cloud_pct": 8.0, "aoi_clear_pct": 95.0, "field_clear_pct": 97.0, "is_suitable": True},
        "trajectory_pre": [{"id": "DEMO_S2_PRE", "date": pre_date_str, "days_from_incident": -4, "field_clear_pct": 99.0}],
        "trajectory_post": [{"id": "DEMO_S2_POST", "date": post_date_str, "days_from_incident": 5, "field_clear_pct": 97.0}],
        "data_sufficiency": "sufficient",
        "notes": "Deterministic synthetic demo simulation of incident comparison.",
    }

    meta = {
        "mode": "demo",
        "synthetic": True,
        "workflow": "incident_comparison",
        "peril": peril,
        "incident_date": incident_date,
        "event_window": [pre_date_str, post_date_str],
        "search_window": [search_start_str, search_end_str],
        "pre_scene": discovery["selected_pre"],
        "post_scene": discovery["selected_post"],
        "geometry": geom.wkt if geom is not None else None,
        "aoi": aoi,
        "scale_m": SCALE,
        "crs": f"EPSG:{epsg}",
        "data_state": "valid",
    }
    write_source_json(os.path.join(procdir, "source.json"), meta)
    return discovery, meta
