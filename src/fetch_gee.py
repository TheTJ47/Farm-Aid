#!/usr/bin/env python3
"""FarmAid - Stage 1: imagery products from Google Earth Engine, for ANY
user-selected location.

The study area ALWAYS follows the current geometry selection: when a
geometry is provided, the AOI is that geometry's bounds buffered by
`aoi_buffer_km` (config; default 2.0 km). The config `aoi_center` is only
the default start location used when no geometry is supplied.

LIVE data states (distinguished, never conflated):
  - "no_matching_imagery"        : zero scenes in the window over the AOI
  - "cloud_filter_rejects_all"   : scenes exist but ALL exceed max_cloud_pct
  - "insufficient_clear_pixels"  : scenes pass the filter, but after
                                   cloud/shadow masking < min clear pixels
  - "valid"                      : usable imagery for analysis

Transparent fallback (config-gated, nothing automatic):
  - baseline_fallback_days / event_fallback_days: widen a failed window by
    ±N days; the ACTUAL dates used are recorded in source.json and reports.
  - baseline years that yield no usable period are SKIPPED and recorded
    (skipped_baseline_years) as long as >= min_baseline_years remain.
  - The cloud filter is NEVER raised automatically; that is the user's
    explicit decision in config.yaml.

Live mode NEVER falls back to synthetic data, never reuses stale rasters,
and never presents the observation composite as the baseline.

Demo mode (--demo): deterministic synthetic rasters FOR THE SELECTED AOI,
always labelled, never a silent fallback for a failed live request.

Usage
  earthengine authenticate
  python src/fetch_gee.py                          # live, config default AOI
  python src/fetch_gee.py --center 28.2654 83.8203 # live, that location
  python src/fetch_gee.py --demo --center 28.2654 83.8203
"""
import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import sys

import numpy as np
import rasterio
import yaml
from pyproj import Transformer
from shapely.geometry import box as make_box

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

NODATA = -9999.0
SCALE = 10  # metres — Sentinel-2 native resolution
COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
RASTER_FILES = ["baseline_mean.tif", "baseline_std.tif",
                "event_ndvi.tif", "event_ndwi.tif"]


class FarmAidDataError(Exception):
    """Raised when the requested data cannot be produced. `state` carries
    the machine-readable data state (see module docstring)."""

    def __init__(self, message, state=None):
        super().__init__(message)
        self.state = state


def load_config(path=None):
    """Load the YAML configuration. If missing, create it from the example
    template. An EXISTING config.yaml is never overwritten."""
    path = path or os.path.join(ROOT, "config.yaml")
    if not os.path.exists(path):
        example = os.path.join(ROOT, "config.example.yaml")
        if os.path.exists(example):
            shutil.copyfile(example, path)
            print(f"[config] Created {path} from config.example.yaml "
                  "(template).")
        else:
            raise FarmAidDataError(
                f"Missing configuration: {path} (and no "
                "config.example.yaml template was found).")
    with open(path) as fh:
        return yaml.safe_load(fh)


def ensure_collection_size(size, label):
    """Guard against empty collections / fully-cloudy windows (raw count)."""
    if size == 0:
        raise FarmAidDataError(
            f"No images matched for `{label}`. Check the date window, widen "
            f"it (+/- 15 days), or raise `max_cloud_pct` (e.g. 60 -> 80). A "
            f"fully cloudy window has nothing to analyse.",
            state="no_matching_imagery")
    return size


# --------------------------------------------------------------------------- #
# AOI / geometry
# --------------------------------------------------------------------------- #
def make_aoi_bounds(cfg):
    """Default AOI (config start location, buffered) — used only when the
    user has not provided a geometry."""
    return selection_to_aoi(cfg, None)


def _center_box(cfg):
    lat, lon = cfg["aoi_center"]
    half = cfg.get("aoi_size_km", 8.0) / 2.0 / 111.0
    half_lon = half / max(np.cos(np.deg2rad(lat)), 0.01)
    return make_box(lon - half_lon, lat - half, lon + half_lon, lat + half)


def geometry_from_center(cfg, lat, lon, size_km=None):
    """A rectangle geometry around (lat, lon)."""
    size_km = size_km or cfg.get("aoi_size_km", 8.0)
    half = size_km / 2.0 / 111.0
    half_lon = half / max(np.cos(np.deg2rad(lat)), 0.01)
    return make_box(lon - half_lon, lat - half, lon + half_lon, lat + half)


def selection_to_aoi(cfg, geom=None):
    """AOI (degrees: [west, south, east, north]) for the SELECTED geometry,
    buffered by aoi_buffer_km; when geom is None, the AOI is built from the
    config default start location. The AOI ALWAYS follows the selection."""
    if geom is None:
        geom = _center_box(cfg)
    buffer_km = cfg.get("aoi_buffer_km", 2.0)
    minx, miny, maxx, maxy = geom.bounds
    half = buffer_km / 111.0
    half_lon = half / max(np.cos(np.deg2rad((miny + maxy) / 2.0)), 0.01)
    bounds = [minx - half_lon, miny - half,
              maxx + half_lon, maxy + half]
    west, south, east, north = bounds
    if not (-85.0 <= south <= north <= 85.0):
        raise FarmAidDataError(
            "Selection latitude is outside the supported range (±85°) — "
            "nothing can be fetched or generated there.")
    if not (-179.9 <= west <= east <= 179.9):
        raise FarmAidDataError(
            "Selection longitude is outside the supported range — the "
            "selection appears to cross the antimeridian, which is not "
            "supported yet.")
    return bounds


def utm_epsg(lon):
    """UTM zone EPSG for the area (northern hemisphere: 326xx)."""
    zone = int((lon + 180.0) / 6.0) + 1
    return 32600 + zone


def write_tiff(path, arr, transform, epsg, nodata=NODATA):
    arr = np.where(np.isfinite(arr), arr, nodata).astype("float32")
    profile = {
        "driver": "GTiff", "height": arr.shape[0], "width": arr.shape[1],
        "count": 1, "dtype": "float32", "crs": f"EPSG:{epsg}",
        "transform": transform, "nodata": nodata,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr, 1)


def write_source_json(path, meta):
    with open(path, "w") as fh:
        json.dump(meta, fh, indent=2)


def project_bounds(west, south, east, north, epsg):
    """Project degree bounds into the UTM CRS; returns projected
    (left, bottom, right, top)."""
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}",
                                       always_xy=True)
    xw, yn = transformer.transform(west, north)
    xe, ys = transformer.transform(east, south)
    return xw, ys, xe, yn


# --------------------------------------------------------------------------- #
# Period diagnostics — pure classifiers (unit-tested; no Earth Engine)
# --------------------------------------------------------------------------- #
def diagnose_period(n_raw, n_filtered, coverage_pct, min_coverage_pct,
                    cloud_pct):
    """Classify one period into the required data states.

    Priority: no imagery at all -> all rejected by the cloud filter ->
    insufficient clear pixels after masking -> valid."""
    if n_raw <= 0:
        return "no_matching_imagery"
    if n_filtered <= 0:
        return "cloud_filter_rejects_all"
    if coverage_pct is not None and coverage_pct < min_coverage_pct:
        return "insufficient_clear_pixels"
    return "valid"


def period_error_message(state, label, n_raw=None, cloud_pct=None,
                         coverage_pct=None, min_cov=None):
    if state == "no_matching_imagery":
        return (f"No Sentinel-2 imagery at all for `{label}` over the "
                "selected AOI. This is a genuine data gap for this "
                "location + window (Sentinel-2 revisits every ~5 days). "
                "Change the dates/baseline years, choose another location, "
                "or enable the config fallback (baseline_fallback_days / "
                "event_fallback_days).")
    if state == "cloud_filter_rejects_all":
        return (f"Imagery EXISTS for `{label}` ({n_raw} scenes) but ALL "
                f"exceed the cloud filter (CLOUDY_PIXEL_PERCENTAGE < "
                f"{cloud_pct}). This is DIFFERENT from 'no imagery': raise "
                f"`max_cloud_pct` in your local config.yaml only if you "
                "accept cloudier scenes — clear-pixel coverage is checked "
                "afterwards.")
    if state == "insufficient_clear_pixels":
        return (f"Imagery exists for `{label}` but after cloud/shadow "
                f"masking only {coverage_pct}% valid pixels remain "
                f"(minimum {min_cov}%). Insufficient clear pixels — try a "
                "different window/location (e.g. post-monsoon October–November "
                "when skies are clear); this is not a credentials issue.")
    return f"Unexpected data state {state!r} for `{label}`."


def raise_period_error(state, label, n_raw=None, cloud_pct=None,
                       coverage_pct=None, min_cov=None):
    raise FarmAidDataError(
        period_error_message(state, label, n_raw=n_raw, cloud_pct=cloud_pct,
                             coverage_pct=coverage_pct, min_cov=min_cov),
        state=state)


# --------------------------------------------------------------------------- #
# Shared GEE helpers (ee imported lazily; module stays importable without it)
# --------------------------------------------------------------------------- #
def init_ee(cfg):
    """Initialise Earth Engine. A missing/import-failing `ee` package or
    missing credentials becomes a clear, actionable FarmAidDataError —
    live mode NEVER falls back to synthetic data."""
    try:
        import ee
    except (ImportError, ModuleNotFoundError):
        raise FarmAidDataError(
            "The 'earthengine-api' package is not installed — run "
            "`pip install -r requirements.txt`. Live mode requires Earth "
            "Engine and has NO synthetic fallback; choose demo mode for "
            "offline work.") from None
    if cfg.get("gee_project"):
        try:
            ee.Initialize(project=cfg["gee_project"])
        except Exception as exc:
            raise FarmAidDataError(
                f"Earth Engine initialisation failed for project "
                f"{cfg['gee_project']!r}: {exc}. Re-run `earthengine "
                "authenticate` once and check the project id in your local "
                "config.yaml.") from None
    else:
        try:
            ee.Initialize()
        except Exception:
            raise FarmAidDataError(
                "Could not initialise Earth Engine with the default project. "
                "Fill `gee_project:` in your local config.yaml (id shown in "
                "code.earthengine.google.com after sign-in) and run "
                "`earthengine authenticate` once.") from None
    return ee


def cloud_mask(img):
    """SCL-based mask (defective/shadow/cloud/cirrus/snow out; QA60 as
    safety net)."""
    scl = img.select("SCL")
    good_scl = (scl.neq(0).And(scl.neq(1)).And(scl.neq(3))
                .And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
                .And(scl.neq(11)))
    qa = img.select("QA60")
    good_qa = (qa.bitwiseAnd(1 << 10).eq(0)
               .And(qa.bitwiseAnd(1 << 11).eq(0)))
    return img.updateMask(good_scl.And(good_qa))


def ndvi_of(img):
    return img.normalizedDifference(["B8", "B4"]).rename("NDVI")


def coverage_pct(ee, img, region, scale):
    cov = (img.mask().reduceRegion(
        ee.Reducer.mean(), region, scale, maxPixels=1e8)
        .values().get(0).getInfo())
    return round(100.0 * (cov or 0.0), 1)


def download_tiff(img, name, region, scale, epsg, outdir):
    import requests
    url = img.unmask(NODATA).clip(region).getDownloadURL({
        "region": region, "scale": scale, "crs": f"EPSG:{epsg}",
        "format": "GEO_TIFF",
    })
    resp = requests.get(url, timeout=600)
    resp.raise_for_status()
    if b"error" in resp.content[:512]:
        raise RuntimeError(f"{name}: {resp.content[:300]!r}")
    path = os.path.join(outdir, name)
    with open(path, "wb") as fh:
        fh.write(resp.content)
    try:
        with rasterio.open(path, "r+") as dst:
            dst.nodata = NODATA
    except Exception:
        pass
    return path


def _date_window(start, end, fallback_days):
    """Optionally widen an ISO date window by ±fallback_days.
    Returns (start, end, fell_back)."""
    if not fallback_days:
        return start, end, False
    s = _dt.date.fromisoformat(start) - _dt.timedelta(days=fallback_days)
    e = _dt.date.fromisoformat(end) + _dt.timedelta(days=fallback_days)
    return s.isoformat(), e.isoformat(), True


def baseline_shortfall_message(n_usable, min_required, skipped):
    reasons = "; ".join(f"{y}: {msg[:80]}" for y, msg in skipped[:5])
    return (f"Only {n_usable} usable baseline year(s) found "
            f"(minimum {min_required} required). Skipped: {reasons}. "
            "Change `baseline_years`/`baseline_window` in your local "
            "config.yaml, or enable `baseline_fallback_days`.")


def query_and_describe(ee, aoi, cfg, start, end, label, fallback_days=0,
                       cloud_pct=None):
    """Query Sentinel-2 for one period over the AOI with full diagnostics.

    Returns (collection, info). Raises FarmAidDataError with a precise
    state for 'no imagery' or 'cloud filter rejects all' — BEFORE any
    composite work. Clear-pixel coverage is checked separately after
    masking (see callers)."""
    cloud_pct = cloud_pct if cloud_pct is not None else cfg["max_cloud_pct"]
    a_start, a_end, fell_back = _date_window(start, end, fallback_days)
    base_col = (ee.ImageCollection(COLLECTION).filterBounds(aoi)
                .filterDate(a_start, a_end))
    n_raw = int(base_col.size().getInfo())
    if n_raw <= 0:
        raise_period_error("no_matching_imagery", label)
    filtered = base_col.filter(
        ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", cloud_pct))
    n_filtered = int(filtered.size().getInfo())
    if n_filtered <= 0:
        raise_period_error("cloud_filter_rejects_all", label,
                           n_raw=n_raw, cloud_pct=cloud_pct)
    t0 = int(filtered.aggregate_min("system:time_start").getInfo()) / 1000.0
    t1 = int(filtered.aggregate_max("system:time_start").getInfo()) / 1000.0
    scenes_diag = []
    try:
        def add_scene_cov(s_img):
            cov = cloud_mask(s_img).select("B4").mask().reduceRegion(
                ee.Reducer.mean(), aoi, 100, maxPixels=1e7
            ).values().get(0)
            return s_img.set("aoi_clear_pct", ee.Number(cov).multiply(100.0))

        with_cov = filtered.map(add_scene_cov)
        raw_info = with_cov.reduceColumns(
            ee.Reducer.toList().repeat(4),
            ["system:index", "system:time_start", "CLOUDY_PIXEL_PERCENTAGE", "aoi_clear_pct"]
        ).get("list").getInfo()
        if raw_info and len(raw_info) == 4:
            for s_id, s_time, s_cloud, s_aoi_clear in zip(raw_info[0], raw_info[1], raw_info[2], raw_info[3]):
                d_str = _dt.datetime.fromtimestamp(
                    s_time / 1000.0, tz=_dt.timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                scenes_diag.append({
                    "id": str(s_id),
                    "date": d_str,
                    "tile_cloud_pct": round(float(s_cloud), 1) if s_cloud is not None else None,
                    "aoi_clear_pct": round(float(s_aoi_clear), 1) if s_aoi_clear is not None else 0.0,
                    "cloud_pct": round(float(s_cloud), 1) if s_cloud is not None else None,
                })
    except Exception:
        try:
            raw_info = filtered.reduceColumns(
                ee.Reducer.toList().repeat(3),
                ["system:index", "system:time_start", "CLOUDY_PIXEL_PERCENTAGE"]
            ).get("list").getInfo()
            if raw_info and len(raw_info) == 3:
                for s_id, s_time, s_cloud in zip(raw_info[0], raw_info[1], raw_info[2]):
                    d_str = _dt.datetime.fromtimestamp(
                        s_time / 1000.0, tz=_dt.timezone.utc
                    ).strftime("%Y-%m-%d %H:%M UTC")
                    scenes_diag.append({
                        "id": str(s_id),
                        "date": d_str,
                        "tile_cloud_pct": round(float(s_cloud), 1) if s_cloud is not None else None,
                        "aoi_clear_pct": None,
                        "cloud_pct": round(float(s_cloud), 1) if s_cloud is not None else None,
                    })
        except Exception:
            pass

    info = {
        "images": n_filtered,
        "raw_images": n_raw,
        "rejected_by_cloud": n_raw - n_filtered,
        "start": a_start, "end": a_end,
        "fallback_days": fallback_days,
        "fallback_used": bool(fell_back),
        "first": _dt.datetime.fromtimestamp(t0, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "last": _dt.datetime.fromtimestamp(t1, tz=_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "scenes": scenes_diag,
    }
    return filtered, info


def _live_cache_key(cfg, aoi):
    payload = json.dumps({
        "aoi": [round(v, 6) for v in aoi],
        "event_window": cfg["event_window"],
        "baseline_years": cfg["baseline_years"],
        "baseline_window": cfg["baseline_window"],
        "max_cloud_pct": cfg["max_cloud_pct"],
    }, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def fetch_from_gee(cfg, procdir, geom=None, cache=True, cache_root=None):
    """Fetch live Sentinel-2 products for the AOI of `geom` (or the config
    default). Returns (meta, reused). Records per-period diagnostics, the
    actual windows used, skipped baseline years and the final data state.
    Raises FarmAidDataError with a precise three-state message when no
    usable imagery exists — never a synthetic fallback, never stale
    rasters, never the observation composite as the baseline."""
    ee = init_ee(cfg)
    aoi = selection_to_aoi(cfg, geom)
    west, south, east, north = aoi
    aoi_ee = ee.Geometry.Rectangle([west, south, east, north])
    geom_ee = None
    if geom is not None:
        try:
            import geopandas as _gpd
            geojson_geom = json.loads(_gpd.GeoSeries([geom]).to_json())["features"][0]["geometry"]
            geom_ee = ee.Geometry(geojson_geom)
        except Exception:
            geom_ee = None
    target_ee = geom_ee if geom_ee is not None else aoi_ee
    epsg = utm_epsg((west + east) / 2.0)

    cache_key = _live_cache_key(cfg, aoi)
    cache_dir = os.path.join(cache_root or os.path.dirname(procdir),
                             "_cache", cache_key)
    reused = False
    if (cache and all(os.path.exists(os.path.join(cache_dir, n))
                      for n in RASTER_FILES)
            and os.path.exists(os.path.join(cache_dir, "source.json"))):
        for n in RASTER_FILES:
            shutil.copyfile(os.path.join(cache_dir, n),
                            os.path.join(procdir, n))
        with open(os.path.join(cache_dir, "source.json")) as fh:
            meta = json.load(fh)
        meta["reused_cache"] = True
        write_source_json(os.path.join(procdir, "source.json"), meta)
        return meta, True

    meta = {
        "mode": "gee", "synthetic": False, "collection": COLLECTION,
        "geometry": geom.wkt if geom is not None else None,
        "aoi": aoi,
        "event_year": cfg["event_year"],
        "event_window": cfg["event_window"],
        "baseline_years": cfg["baseline_years"],
        "baseline_window": cfg["baseline_window"],
        "scale_m": SCALE, "crs": f"EPSG:{epsg}",
        "max_cloud_pct": cfg["max_cloud_pct"],
        "min_period_coverage_pct": cfg.get("min_period_coverage_pct", 20.0),
        "baseline_fallback_days": cfg.get("baseline_fallback_days", 0),
        "event_fallback_days": cfg.get("event_fallback_days", 0),
    }

    print(f"Live query: AOI {[round(v, 4) for v in aoi]}, "
          f"collection {COLLECTION}")
    baseline_ndvis = []
    meta["baseline_periods"] = []
    meta["skipped_baseline_years"] = []
    min_cov = cfg.get("min_period_coverage_pct", 20.0)
    min_baseline = cfg.get("min_baseline_years", 2)

    for year in cfg["baseline_years"]:
        ws, we = cfg["baseline_window"]
        label = f"baseline {year}"
        try:
            col, info = query_and_describe(
                ee, aoi_ee, cfg, f"{year}-{ws}", f"{year}-{we}",
                label,
                fallback_days=cfg.get("baseline_fallback_days", 0))
            comp = col.map(cloud_mask).median()
            ndvi_img = ndvi_of(comp)
            info["coverage_pct"] = coverage_pct(ee, ndvi_img, aoi_ee,
                                                SCALE)
            if info["coverage_pct"] < min_cov:
                raise_period_error("insufficient_clear_pixels", label,
                                    coverage_pct=info["coverage_pct"],
                                    min_cov=min_cov)
            baseline_ndvis.append(ndvi_img)
            meta["baseline_periods"].append({"year": year, **info})
            print(f"  {label}: {info['images']}/{info['raw_images']} "
                  f"images (cloud-rejected {info['rejected_by_cloud']}), "
                  f"coverage {info['coverage_pct']}%"
                  + (" [fallback window used]" if info["fallback_used"]
                     else ""))
        except FarmAidDataError as exc:
            meta["skipped_baseline_years"].append(
                {"year": year, "reason": str(exc)[:200],
                 "state": exc.state})
            print(f"  {label}: SKIPPED — {exc}")
            continue

    if len(baseline_ndvis) < min_baseline:
        raise FarmAidDataError(
            baseline_shortfall_message(
                len(baseline_ndvis), min_baseline,
                [(y["year"], y["reason"])
                 for y in meta["skipped_baseline_years"]]),
            state="insufficient_clear_pixels")

    coll = ee.ImageCollection.fromImages(baseline_ndvis)
    mean_img = coll.reduce(ee.Reducer.mean()).rename("mean")
    std_img = coll.reduce(ee.Reducer.stdDev()).rename("std")

    # --- event-season composite (hard requirement: no event -> nothing) ---
    ws, we = cfg["event_window"]
    ev_col, ev_info = query_and_describe(
        ee, aoi_ee, cfg, ws, we, "event window",
        fallback_days=cfg.get("event_fallback_days", 0))
    ev_comp = ev_col.map(cloud_mask).median()
    ev_ndvi = ndvi_of(ev_comp)
    ev_ndwi = ev_comp.normalizedDifference(["B3", "B8"]).rename("NDWI")
    ev_info["coverage_pct"] = coverage_pct(ee, ev_ndvi, aoi_ee, SCALE)
    if geom_ee is not None:
        try:
            ev_info["field_coverage_pct"] = coverage_pct(ee, ev_ndvi, geom_ee, SCALE)
        except Exception:
            pass
    if ev_info["coverage_pct"] < min_cov:
        raise_period_error("insufficient_clear_pixels", "event window",
                           coverage_pct=ev_info["coverage_pct"],
                           min_cov=min_cov)
    meta["event"] = ev_info
    meta["data_state"] = "valid"
    print(f"  event: {ev_info['images']}/{ev_info['raw_images']} images "
          f"(cloud-rejected {ev_info['rejected_by_cloud']}), "
          f"coverage {ev_info['coverage_pct']}%")

    for img, name in ((mean_img, "baseline_mean.tif"),
                      (std_img, "baseline_std.tif"),
                      (ev_ndvi, "event_ndvi.tif"),
                      (ev_ndwi, "event_ndwi.tif")):
        try:
            download_tiff(img, name, aoi_ee, SCALE, epsg, procdir)
        except Exception as exc:
            raise FarmAidDataError(
                f"Earth Engine export failed for {name}: {exc}. "
                f"Try again later or reduce the study area size.") from exc

    write_source_json(os.path.join(procdir, "source.json"), meta)
    if cache:
        os.makedirs(cache_dir, exist_ok=True)
        for n in RASTER_FILES:
            shutil.copyfile(os.path.join(procdir, n),
                            os.path.join(cache_dir, n))
        write_source_json(os.path.join(cache_dir, "source.json"), meta)
    return meta, False


# --------------------------------------------------------------------------- #
# DEMO mode — deterministic synthetic rasters FOR THE SELECTED AOI.
# --------------------------------------------------------------------------- #
def _demo_seed(cfg, aoi):
    return hashlib.sha1(
        json.dumps([round(v, 6) for v in aoi],
                   sort_keys=True).encode("utf-8")).hexdigest()


def fetch_demo(cfg, procdir, geom=None, seed=None):
    print("DEMO MODE: deterministic SYNTHETIC dataset (not satellite "
          "observations).")
    aoi = selection_to_aoi(cfg, geom)
    west, south, east, north = aoi
    epsg = utm_epsg((west + east) / 2.0)
    xw, ys, xe, yn = project_bounds(west, south, east, north, epsg)
    cols = max(int(round((xe - xw) / SCALE)), 1)
    rows = max(int(round((yn - ys) / SCALE)), 1)
    transform = rasterio.transform.from_origin(xw, yn, SCALE, SCALE)

    seed_hex = seed or _demo_seed(cfg, aoi)
    rng = np.random.default_rng(int(seed_hex[:8], 16))

    yy, xx = np.mgrid[0:rows, 0:cols].astype(float)
    mean = np.full((rows, cols), 0.62) + rng.normal(0, 0.012, (rows, cols))
    std = np.full((rows, cols), 0.050) + rng.normal(0, 0.008, (rows, cols))
    ndvi = mean.copy()
    ndwi = np.full((rows, cols), -0.05)

    # synthetic stress patches tied to THIS raster's extent (pixel units).
    # First patch = flood, second = drought (guarantees both cause types
    # exist in every demo scene for the regression tests); the rest random.
    n_patches = int(rng.integers(4, 7))
    for i in range(n_patches):
        fy, fx = rng.uniform(0.15, 0.85, size=2)
        radius = int(rng.integers(25, 55))
        if i == 0:
            is_flood = True
        elif i == 1:
            is_flood = False
        else:
            is_flood = bool(rng.random() < 0.55)
        py, px = rows * fy, cols * fx
        d = np.sqrt((yy - py) ** 2 + (xx - px) ** 2)
        m = d < radius
        if is_flood:
            dip = np.clip(0.32 * (1 - d / (2 * radius)), 0, 0.35)
            ndvi = np.where(m, mean - dip, ndvi)
            water = np.clip(0.16 * (1 - d / (1.5 * radius)), 0, 0.2)
            ndwi = np.where(m, ndwi + water + 0.02, ndwi)
        else:
            dip = np.clip(0.24 * (1 - d / (2 * radius)), 0, 0.26)
            ndvi = np.where(m, mean - dip, ndvi)
            ndwi = np.where(m, ndwi - 0.03, ndwi)

    write_tiff(os.path.join(procdir, "baseline_mean.tif"), mean,
               transform, epsg)
    write_tiff(os.path.join(procdir, "baseline_std.tif"), std,
               transform, epsg)
    write_tiff(os.path.join(procdir, "event_ndvi.tif"), ndvi,
               transform, epsg)
    write_tiff(os.path.join(procdir, "event_ndwi.tif"), ndwi,
               transform, epsg)

    meta = {
        "mode": "demo", "synthetic": True,
        "demo_note": "Deterministic synthetic dataset for offline "
                     "development. NOT satellite observations; do not "
                     "present as live results.",
        "seed": seed_hex,
        "collection": None,
        "geometry": geom.wkt if geom is not None else None,
        "aoi": aoi,
        "event_year": cfg["event_year"], "event_window": cfg["event_window"],
        "baseline_years": cfg["baseline_years"],
        "baseline_window": cfg["baseline_window"],
        "scale_m": SCALE, "crs": f"EPSG:{epsg}",
        "projected_bounds_m": [round(xw, 1), round(ys, 1),
                               round(xe, 1), round(yn, 1)],
        "grid": [rows, cols],
    }
    write_source_json(os.path.join(procdir, "source.json"), meta)
    print(f"  synthetic raster {rows}x{cols} @10 m, CRS EPSG:{epsg} "
          f"-> {procdir}")
    return meta, {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--demo", action="store_true",
                    help="generate labelled synthetic rasters for the AOI "
                         "(never a fallback for live mode)")
    ap.add_argument("--center", nargs=2, type=float,
                    metavar=("LAT", "LON"),
                    help="location to build the AOI around (default: "
                         "config aoi_center)")
    ap.add_argument("--size-km", type=float,
                    help="AOI side (km) for --center (default: "
                         "aoi_size_km)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    geom = None
    if args.center:
        geom = geometry_from_center(cfg, *args.center, args.size_km)
    procdir = os.path.join(ROOT, cfg["data_dir"], "processed",
                           "default" if geom is None else "cli")
    os.makedirs(procdir, exist_ok=True)

    if args.demo:
        fetch_demo(cfg, procdir, geom=geom)
    else:
        meta, _reused = fetch_from_gee(cfg, procdir, geom=geom)
        ev = meta.get("event", {})
        print(f"  event: {ev.get('images', '?')} images, "
              f"coverage {ev.get('coverage_pct', '?')}% · "
              f"data_state={meta.get('data_state')}")
    print("Stage 1 done. Next: python src/analyze.py")


if __name__ == "__main__":
    main()