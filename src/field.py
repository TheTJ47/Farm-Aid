#!/usr/bin/env python3
"""FarmAid - Stage 3: farmer-facing FIELD assessment for the EXACT
user-selected geometry, anywhere.

The study area follows the selection: the AOI is the selected geometry
buffered by `aoi_buffer_km`, and the rasters (live Earth Engine or
deterministic synthetic demo) are built for that AOI — so the field is
always inside the fetched footprint by construction, and partial coverage
surfaces as a valid-pixel fraction in the metrics, never as silent
cropping.

Every assessment carries a run_id + parameter fingerprint (runmeta), so
results can never be confused across geometries, dates, baselines or
modes. A rasters directory from a different selection is rejected.

Semantics (per spec):
  - insufficient_data : valid coverage < min_valid_coverage_pct -> no
                        reliable assessment; cause=unknown, high uncertainty
  - no_evidence_of_damage : sufficient data, no anomaly detected
  - affected area is always a SCREENING estimate, never crop loss
  - hail: unsupported, never returned

CLI
  python src/field.py --coords <lat> <lon> <size_km> [--live]
  python src/field.py --geojson boundary.geojson [--live]
"""
import argparse
import json
import math
import os
import sys

import numpy as np
import geopandas as gpd
import rasterio
from shapely.geometry import MultiPolygon, Polygon, box as make_box
from shapely.geometry import shape as to_shape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

import analyze  # noqa: E402  (pure score functions)
from analyze import anomaly_z, classify_cause, classify_severity  # noqa: E402
import fetch_gee  # noqa: E402
from fetch_gee import (FarmAidDataError, load_config,  # noqa: E402
                       utm_epsg)
from runmeta import canonical_settings, new_run_id, param_key  # noqa: E402

FILES = ["baseline_mean.tif", "baseline_std.tif",
         "event_ndvi.tif", "event_ndwi.tif"]


# --------------------------------------------------------------------------- #
# Geometry handling
# --------------------------------------------------------------------------- #
def validate_geometry(geom):
    if not isinstance(geom, (Polygon, MultiPolygon)):
        raise FarmAidDataError(
            "Boundary must be a polygon (coordinates box or drawn polygon).")
    if geom.is_empty or not geom.is_valid:
        raise FarmAidDataError(
            "Boundary is empty or invalid — please draw it again.")
    if geom.area < 1e-10:
        raise FarmAidDataError(
            "Boundary area is zero — select a larger field.")
    return geom


def geometry_from_inputs(*args, coords=None, geojson_path=None, lat=None, lon=None, size_km=None):
    """Build a validated WGS84 polygon from CLI inputs or coordinates."""
    if len(args) == 1 and isinstance(args[0], (list, tuple)):
        coords = args[0]
    elif len(args) == 2:
        lat, lon = args
    elif len(args) == 3:
        lat, lon, size_km = args
    elif len(args) == 1 and isinstance(args[0], str):
        geojson_path = args[0]

    if geojson_path and isinstance(geojson_path, (str, os.PathLike)):
        if not os.path.exists(geojson_path):
            raise FarmAidDataError(f"No such file: {geojson_path}")
        with open(geojson_path) as fh:
            data = json.load(fh)
        geom = to_shape(data.get("geometry") or data)
    elif coords or (lat is not None and lon is not None and size_km is not None):
        if coords:
            lat, lon, size_km = coords
        half = size_km / 2.0 / 111.0
        half_lon = half / max(np.cos(np.deg2rad(lat)), 0.01)
        geom = make_box(lon - half_lon, lat - half,
                        lon + half_lon, lat + half)
    else:
        raise FarmAidDataError("Provide --coords, lat/lon/size_km, or --geojson.")
    return validate_geometry(geom)


def area_ha(geom):
    """Plane area of a WGS84 polygon in hectares (UTM-projected)."""
    epsg = utm_epsg(geom.centroid.x)
    return float(gpd.GeoSeries([geom], crs="EPSG:4326")
                 .to_crs(epsg=epsg).area[0]) / 1e4


# --------------------------------------------------------------------------- #
# JSON-safe map properties — folium serializes feature properties with
# json.dumps, which cannot handle numpy arrays/scalars or non-finite
# numbers. These helpers build a CLEAN display-only representation; the
# authoritative metrics/GeoJSON are never mutated.
# --------------------------------------------------------------------------- #
def geojson_safe_value(value):
    """Recursively convert a property value to JSON-compatible form:
    numpy arrays -> lists, numpy scalars -> native Python values,
    tuple -> list, dict recursed, non-finite floats -> None,
    timestamps/dates -> ISO strings."""
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (bool, str)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(k): geojson_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [geojson_safe_value(v) for v in value]
    if isinstance(value, np.generic):  # np.float64/int64/bool_/...
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return None if not math.isfinite(value) else value
    return str(value)


def map_safe_geodataframe(gdf, drop_columns=()):
    """Copy of `gdf` with JSON-clean properties for map serialization.
    Internal columns named in drop_columns are removed; geometry and
    meaningful assessment attributes are preserved. Input is not mutated."""
    out = gdf.copy()
    drop = [c for c in drop_columns if c in out.columns]
    if drop:
        out = out.drop(columns=drop)
    import pandas as pd
    for col in out.columns:
        if col == out.geometry.name:
            continue
        out[col] = pd.Series([geojson_safe_value(v) for v in out[col]], index=out.index, dtype=object)
    return out


# --------------------------------------------------------------------------- #
# Scoring (shared with the grid pipeline's rules)
# --------------------------------------------------------------------------- #
def _as_masked(a):
    """Accept masked OR plain numpy arrays; always return a masked array
    with invalid (nan/inf) and nodata values masked."""
    a = np.ma.asarray(a)
    mask = ~np.isfinite(a) | (a == -9999.0)
    if np.ma.is_masked(a):
        mask = mask | a.mask
    return np.ma.masked_where(mask, a)


def score_arrays(ndvi_m, ndwi_m, mean_m, std_m, cfg, area_ha_, n_field_pixels=None, claimed_peril=None):
    """Field-level metrics from masked arrays. Measured vs heuristic kept
    separate. Denominators: affected_pct uses VALID pixels; valid_pct uses
    pixels of the field geometry."""
    ndvi_m, ndwi_m, mean_m, std_m = (
        _as_masked(a) for a in (ndvi_m, ndwi_m, mean_m, std_m))
    z = anomaly_z(ndvi_m, mean_m, std_m)
    threshold = -abs(cfg["ndvi_z_threshold"])

    # Co-located valid mask: pixels where both event NDVI and baseline (mean+std) are valid
    valid_mask = ~np.ma.getmaskarray(z)
    n_valid = int(np.count_nonzero(valid_mask))
    denom = n_field_pixels if n_field_pixels is not None else int(ndvi_m.size)
    valid_pct = 100.0 * n_valid / max(denom, 1)

    if n_valid > 0:
        ndvi_valid = np.asarray(ndvi_m)[valid_mask]
        mean_valid = np.asarray(mean_m)[valid_mask]
        z_valid = np.asarray(z)[valid_mask]

        affected_arr = (z_valid < threshold) & (ndvi_valid < mean_valid)
        frac = float(np.mean(affected_arr)) if len(affected_arr) else 0.0

        ndvi_mean_c = float(np.mean(ndvi_valid)) if len(ndvi_valid) else np.nan
        b_mean_c = float(np.mean(mean_valid)) if len(mean_valid) else np.nan
        delta = ndvi_mean_c - b_mean_c if (np.isfinite(ndvi_mean_c) and np.isfinite(b_mean_c)) else np.nan
        z_mean = float(np.mean(z_valid)) if len(z_valid) else np.nan

        ndwi_arr = np.asarray(ndwi_m)
        ndwi_valid_mask = valid_mask & ~np.ma.getmaskarray(ndwi_m)
        if np.any(ndwi_valid_mask):
            ndwi_mean_c = float(np.mean(ndwi_arr[ndwi_valid_mask]))
        elif ndwi_m.count():
            ndwi_mean_c = float(np.ma.mean(ndwi_m))
        else:
            ndwi_mean_c = np.nan
    else:
        frac = 0.0
        ndvi_mean_c = np.nan
        b_mean_c = float(np.ma.mean(mean_m)) if mean_m.count() else np.nan
        delta = np.nan
        z_mean = np.nan
        ndwi_mean_c = np.nan

    severity = classify_severity(frac, cfg["severity"])
    cause, uncertainty = classify_cause(frac, ndwi_mean_c, delta,
                                        cfg["ndwi_flood_threshold"],
                                        claimed_peril=claimed_peril)

    return {
        "area_ha": round(area_ha_, 3),
        "valid_pct": round(min(valid_pct, 100.0), 1),
        "affected_pct": round(frac * 100.0, 1),
        "affected_area_ha": round(frac * area_ha_, 3),  # SCREENING estimate
        "ndvi_mean": round(ndvi_mean_c, 3) if np.isfinite(ndvi_mean_c) else None,
        "baseline_ndvi_mean": round(b_mean_c, 3) if np.isfinite(b_mean_c) else None,
        "ndvi_delta": round(delta, 3) if np.isfinite(delta) else None,
        "z_score": round(z_mean, 2) if np.isfinite(z_mean) else None,
        "ndwi_mean": round(ndwi_mean_c, 3) if np.isfinite(ndwi_mean_c) else None,
        "severity": severity,
        "cause": cause,
        "uncertainty": uncertainty,
        "n_valid_pixels": n_valid,
        "n_field_pixels": denom,
    }


def sufficiency_flag(valid_pct, cfg):
    """True when valid coverage is too low for a reliable assessment."""
    return valid_pct < cfg.get("min_valid_coverage_pct", 30.0)


def evidence_state(metrics, cfg):
    """Classify the evidence for a scored field:
      - "insufficient" : valid coverage below the minimum -> no conclusion
      - "no_evidence"  : sufficient data, no negative anomaly
      - "assessed"     : sufficient data with an anomaly assessment
    """
    if sufficiency_flag(metrics["valid_pct"], cfg):
        return "insufficient"
    if metrics.get("cause") == "none" and metrics.get("affected_pct", 0) < 0.5:
        return "no_evidence"
    if metrics.get("affected_pct", 0) < 0.5 and metrics.get("severity") == "none":
        return "no_evidence"
    return "assessed"


# --------------------------------------------------------------------------- #
# Raster acquisition for the SELECTED boundary (AOI follows the selection)
# --------------------------------------------------------------------------- #
def prepare_rasters(cfg, geom, mode, root, run_id, seed=None):
    """Fetch (live) or generate (demo) rasters for the AOI of `geom`.
    Returns (meta, procdir)."""
    procdir = os.path.join(root, cfg["data_dir"], "processed", run_id)
    os.makedirs(procdir, exist_ok=True)
    if mode == "live":
        meta, _reused = fetch_gee.fetch_from_gee(cfg, procdir, geom=geom,
                                                 cache=True,
                                                 cache_root=os.path.join(
                                                     root, cfg["data_dir"],
                                                     "processed"))
    else:
        meta, _ = fetch_gee.fetch_demo(cfg, procdir, geom=geom, seed=seed)
    meta["run_id"] = run_id
    return meta, procdir


def read_field_rasters(paths, geom=None):
    """Read field rasters. When `geom` is provided, clips and masks the arrays
    to the exact field boundary, ensuring that valid coverage, anomaly percentages,
    and pixel counts represent ONLY the farmer's selected field (never the surrounding AOI).
    """
    import rasterio.mask
    import rasterio.features
    from shapely.geometry import mapping

    field_pixel_count = None

    def _read(path):
        nonlocal field_pixel_count
        with rasterio.open(path) as src:
            nodata_val = src.nodata if src.nodata is not None else -9999.0
            if geom is not None:
                epsg = src.crs.to_epsg() or utm_epsg(geom.centroid.x)
                gdf_geom = gpd.GeoSeries([geom], crs="EPSG:4326").to_crs(epsg=epsg)
                geom_proj = gdf_geom.iloc[0]
                out_img, out_transform = rasterio.mask.mask(
                    src, [mapping(geom_proj)], crop=True, all_touched=True,
                    nodata=nodata_val
                )
                arr = out_img[0]
                if field_pixel_count is None:
                    inside = rasterio.features.geometry_mask(
                        [mapping(geom_proj)], out_shape=arr.shape,
                        transform=out_transform, invert=True, all_touched=True
                    )
                    field_pixel_count = int(np.count_nonzero(inside))
            else:
                arr = src.read(1)
            mask = (arr == nodata_val) | (arr == -9999.0) | ~np.isfinite(arr)
            if src.nodata is not None:
                mask = mask | (arr == src.nodata)
            return np.ma.masked_where(mask, arr)

    mean = _read(paths["baseline_mean.tif"])
    std = _read(paths["baseline_std.tif"])
    ndvi = _read(paths["event_ndvi.tif"])
    ndwi = _read(paths["event_ndwi.tif"])
    for a in (std, ndvi, ndwi):
        if a.shape != mean.shape:
            raise FarmAidDataError(
                "Field rasters are misaligned — re-run the fetch step.")
    return {
        "mean": mean, "std": std, "ndvi": ndvi, "ndwi": ndwi,
        "n_field_pixels": field_pixel_count
    }


def make_field_quicklook(mean_m, ndvi_m, affected, out_path, meta=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(14, 4.5))

    cmap_veg = plt.cm.YlGn.with_extremes(bad="#EBEBEB")
    cmap_aff = plt.cm.YlOrRd.with_extremes(bad="#EBEBEB")

    is_incident = (meta or {}).get("workflow") == "incident_comparison"
    pre_date = (meta or {}).get("pre_scene", {}).get("date", "Pre-Incident")
    post_date = (meta or {}).get("post_scene", {}).get("date", "Post-Incident")

    if is_incident:
        title0 = f"Pre-Incident Observation\n({pre_date} Sentinel-2 NDVI)"
        title1 = f"Post-Incident Observation\n({post_date} Sentinel-2 NDVI)"
        label_bar0 = "Pre-Incident NDVI"
        label_bar1 = "Post-Incident NDVI"
        main_title = f"Incident Before-and-After Evidence — Sentinel-2 Crop Damage Analysis ({meta.get('peril', 'Claimed Peril')})"
    else:
        title0 = "Historical Baseline Norm\n(Healthy Multi-Year Baseline NDVI)"
        title1 = "Claim Observation Period\n(Observed Sentinel-2 NDVI)"
        label_bar0 = "NDVI (Greener = healthier norm)"
        label_bar1 = "Observed NDVI"
        main_title = "Satellite Field Evidence — Sentinel-2 Crop Damage Screening (Grey = clouds, shadow, or outside boundary)"

    # Panel 1: Pre-Incident / Baseline
    im0 = axes[0].imshow(mean_m, cmap=cmap_veg, vmin=0.05, vmax=0.8)
    axes[0].set_title(title0, fontsize=11, fontweight="bold", pad=8)
    axes[0].set_xticks([])
    axes[0].set_yticks([])
    cbar0 = plt.colorbar(im0, ax=axes[0], fraction=0.046, pad=0.04)
    cbar0.set_label(label_bar0, fontsize=9)

    # Panel 2: Post-Incident / Observed
    im1 = axes[1].imshow(ndvi_m, cmap=cmap_veg, vmin=0.05, vmax=0.8)
    axes[1].set_title(title1, fontsize=11, fontweight="bold", pad=8)
    axes[1].set_xticks([])
    axes[1].set_yticks([])
    cbar1 = plt.colorbar(im1, ax=axes[1], fraction=0.046, pad=0.04)
    cbar1.set_label(label_bar1, fontsize=9)

    # Panel 3: Damage Screening Indicator
    im2 = axes[2].imshow(affected.astype(float), cmap=cmap_aff, vmin=0, vmax=1)
    axes[2].set_title("Damage Screening Indicator\n(Flagged Negative Anomaly: z < -1.5)",
                      fontsize=11, fontweight="bold", pad=8)
    axes[2].set_xticks([])
    axes[2].set_yticks([])
    cbar2 = plt.colorbar(im2, ax=axes[2], fraction=0.046, pad=0.04, ticks=[0, 1])
    cbar2.ax.set_yticklabels(["Normal / No stress", "Flagged stress"])

    fig.suptitle(main_title, fontsize=10, y=0.98, color="#444444")
    plt.tight_layout(rect=(0, 0, 1, 0.94))
    plt.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path


# --------------------------------------------------------------------------- #
# End-to-end assessment of the exact selected geometry
# --------------------------------------------------------------------------- #
def assess_field(cfg, geom, mode, root=ROOT, run_id=None, rasters_dir=None,
                 meta=None, seed=None, claim_info=None, **kwargs):
    """Run the full field workflow for the SELECTED geometry.

    - mode: 'live' (Earth Engine, cached) | 'demo' (deterministic
      synthetic for THIS location)
    - When rasters_dir is given it must belong to this selection
      (stale-guard); otherwise rasters are fetched/generated for the AOI
      of `geom`.
    """
    geom = validate_geometry(geom)
    run_id = run_id or new_run_id()
    data_dir = os.path.join(root, cfg.get("data_dir", "data"))

    if rasters_dir is None:
        meta, procdir = prepare_rasters(cfg, geom, mode, root, run_id,
                                        seed=seed)
    else:
        procdir = rasters_dir
        meta_file = os.path.join(procdir, "source.json")
        meta = meta or {}
        if os.path.exists(meta_file):
            with open(meta_file) as fh:
                meta = json.load(fh)
        recorded = meta.get("geometry")
        if recorded and recorded != geom.wkt:
            raise FarmAidDataError(
                "The raster cache belongs to a DIFFERENT geometry than the "
                "current selection. Re-run the assessment so results match "
                "the selected field (a new AOI will be fetched/generated).")

    paths = {n: os.path.join(procdir, n) for n in FILES}
    for n, p in paths.items():
        if not os.path.exists(p):
            raise FarmAidDataError(
                f"Missing raster {p} — run the fetch step for the current "
                f"selection first.")

    arrays = read_field_rasters(paths, geom=geom)
    ha = area_ha(geom)
    c_peril = (claim_info or {}).get("claimed_cause") or meta.get("peril")
    metrics = score_arrays(arrays["ndvi"], arrays["ndwi"],
                           arrays["mean"], arrays["std"], cfg, ha,
                           n_field_pixels=arrays.get("n_field_pixels"),
                           claimed_peril=c_peril)
    metrics["mode"] = mode
    metrics["run_id"] = run_id
    metrics["geometry_wkt"] = geom.wkt
    metrics["bounds_wgs84"] = [round(v, 6) for v in geom.bounds]
    metrics["centroid_wgs84"] = [round(geom.centroid.y, 6),
                                 round(geom.centroid.x, 6)]
    metrics["claim_info"] = claim_info or {}
    metrics["workflow"] = meta.get("workflow", "baseline_composite")
    metrics["event_window"] = meta.get("event_window") or cfg.get("event_window")
    metrics["incident_date"] = (claim_info or {}).get("incident_date") or meta.get("incident_date")
    metrics["search_window"] = meta.get("search_window") or cfg.get("search_window")

    if meta.get("workflow") == "incident_comparison":
        inc_str = str(metrics["incident_date"] or "none")
        wf_mode = (claim_info or {}).get("workflow_mode") or meta.get("workflow_mode", "historical")
        c_peril_str = c_peril or "General"
        metrics["param_key"] = param_key(
            mode, geom.wkt, [inc_str, wf_mode, c_peril_str],
            cfg.get("baseline_years", []), canonical_settings(cfg))
    else:
        metrics["param_key"] = param_key(
            mode, geom.wkt, cfg.get("event_window", ["none", "none"]),
            cfg.get("baseline_years", []), canonical_settings(cfg))

    # evidence: distinguish "no evidence" from "not enough data"
    state = evidence_state(metrics, cfg)
    metrics["evidence"] = state
    metrics["insufficient_data"] = (state == "insufficient")
    if state == "insufficient":
        metrics["severity"] = "unassessable"
        metrics["cause"] = "unknown"
        metrics["uncertainty"] = "high"
        metrics["note"] = (
            f"Insufficient valid observations ({metrics['valid_pct']}% < "
            f"{cfg.get('min_valid_coverage_pct', 30.0)}%) — no reliable "
            "assessment for this field; clouds/empty imagery are likely.")
    else:
        metrics["no_evidence_of_damage"] = (state == "no_evidence")

    # outputs (all inside this run's directory — stale outputs can't be
    # confused with a new run)
    outdir = os.path.join(root, cfg.get("out_dir", "data/outputs"), run_id)
    os.makedirs(outdir, exist_ok=True)

    geometry_props = {k: geojson_safe_value(v) for k, v in metrics.items()}
    gdf_out = gpd.GeoDataFrame([{**geometry_props, "geometry": geom}],
                               crs="EPSG:4326")
    geojson_path = os.path.join(outdir, "field_result.geojson")
    gdf_out.to_file(geojson_path, driver="GeoJSON")

    metrics_path = os.path.join(outdir, "field_metrics.json")
    with open(metrics_path, "w") as fh:
        json.dump({"run_id": run_id, "param_key": metrics["param_key"],
                   "metrics": metrics, "acquisition": meta,
                   "claim_info": claim_info or {}}, fh, indent=2)

    z = anomaly_z(arrays["ndvi"], arrays["mean"], arrays["std"])
    affected = ((z < -abs(cfg["ndvi_z_threshold"]))
                & (arrays["ndvi"] < arrays["mean"]))
    quicklook = os.path.join(outdir, "field_quicklook.png")
    make_field_quicklook(arrays["mean"], arrays["ndvi"], affected, quicklook, meta=meta)

    import report
    pdf_dir = os.path.join(root, cfg.get("report_dir", "reports"), run_id)
    os.makedirs(pdf_dir, exist_ok=True)
    pdf = os.path.join(pdf_dir, f"FarmAid_field_report_{run_id}.pdf")
    report.build_field_report(cfg, metrics, meta, quicklook, pdf, claim_info=claim_info)

    return {
        "run_id": run_id,
        "param_key": metrics["param_key"],
        "metrics": metrics,
        "geojson": geojson_path,
        "metrics_json": metrics_path,
        "quicklook": quicklook,
        "pdf": pdf,
        "outdir": outdir,
        "procdir": procdir,
        "meta": meta,
        "claim_info": claim_info or {},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--coords", nargs=3, type=float, metavar=("LAT", "LON",
                                                              "SIZE_KM"))
    ap.add_argument("--geojson", help="path to a GeoJSON boundary file")
    ap.add_argument("--live", action="store_true",
                    help="query Earth Engine for the boundary (requires "
                         "auth); default is demo mode")
    ap.add_argument("--claim-ref", default=None, help="Insurance claim reference number")
    ap.add_argument("--policy-no", default=None, help="Policy number")
    ap.add_argument("--claimant-name", default=None, help="Claimant / insured name")
    ap.add_argument("--crop-type", default=None, help="Insured crop type")
    ap.add_argument("--peril", default=None, help="Claimed peril / cause")
    ap.add_argument("--incident-date", default=None, help="Claimed incident date (YYYY-MM-DD)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    geom = geometry_from_inputs(coords=args.coords, geojson_path=args.geojson)
    mode = "live" if args.live else "demo"
    claim_info = None
    if any([args.claim_ref, args.policy_no, args.claimant_name, args.crop_type, args.peril, args.incident_date]):
        claim_info = {
            "claim_ref": args.claim_ref or "Not provided",
            "policy_no": args.policy_no or "Not provided",
            "claimant_name": args.claimant_name or "Not provided",
            "crop_type": args.crop_type or "Not specified",
            "claimed_cause": args.peril or "General Crop Stress",
            "incident_date": args.incident_date,
        }
    res = assess_field(cfg, geom, mode, claim_info=claim_info)
    m = res["metrics"]
    print(f"\nField assessment ({mode}) run {res['run_id']}")
    print(f"  geometry       : {geom.wkt[:80]}…")
    print(f"  area           : {m['area_ha']} ha")
    print(f"  valid coverage : {m['valid_pct']}%"
          + ("  -> INSUFFICIENT DATA" if m.get("insufficient_data")
             else ""))
    print(f"  affected       : {m['affected_pct']}% of valid pixels "
          f"(~{m['affected_area_ha']} ha screening estimate)")
    print(f"  severity       : {m['severity']}  |  cause: {m['cause']} "
          f"(uncertainty {m['uncertainty']})")
    print("  outputs        :", res["geojson"], "/", res["quicklook"],
          "/", res["pdf"])


if __name__ == "__main__":
    main()