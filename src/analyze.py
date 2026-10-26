#!/usr/bin/env python3
"""FarmAid - Stage 2: anomaly scoring + analytical-grid assessment.

Reads the rasters of ONE run (which were built for the CURRENT selection's
AOI) and writes the grid outputs into that run's output directory:

  damage.geojson         analytical cells (never farm parcels) + metrics
  damage_summary.csv
  quicklook.png          screening visual

The grid is derived from the ACTUAL raster bounds of the run — a fixed
analysis grid is never shown for a different location.

Pure scoring functions (unit-tested): ndvi, ndwi, anomaly_z, mask_nodata,
classify_severity, classify_cause.

Usage:
  python src/analyze.py [--processed-dir DIR] [--out-dir DIR]
"""
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
from rasterio.windows import from_bounds
from shapely.geometry import box

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from fetch_gee import FarmAidDataError, load_config  # noqa: E402

NODATA = -9999.0
EPS = 1e-9
FILES = ["baseline_mean.tif", "baseline_std.tif",
         "event_ndvi.tif", "event_ndwi.tif"]


# --------------------------------------------------------------------------- #
# Pure, importable logic (unit-tested in tests/test_pipeline.py)
# --------------------------------------------------------------------------- #
def ndvi(nir, red):
    """NDVI = (NIR - Red) / (NIR + Red)."""
    return (nir - red) / (nir + red + EPS)


def ndwi(green, nir):
    """NDWI = (Green - NIR) / (Green + NIR)  (McFeeters 1996).
    Water-related indicator only; not proof of flooding by itself."""
    return (green - nir) / (green + nir + EPS)


def anomaly_z(observed, mean, std, floor=0.02):
    """z = (observed - mean) / max(std, floor). Guards zero/near-zero std."""
    return (observed - mean) / np.maximum(std, floor)


def mask_nodata(arr, nodata=NODATA):
    """Mask nodata / invalid pixels for statistics."""
    mask = (arr == nodata) | ~np.isfinite(arr)
    return np.ma.masked_where(mask, arr)


def classify_severity(frac, cut):
    """Provisional severity from affected-valid-pixel fraction (lower bounds
    in `cut`: low/moderate/high). Returns none|low|moderate|high."""
    if frac >= cut["high"]:
        return "high"
    if frac >= cut["moderate"]:
        return "moderate"
    if frac >= cut["low"]:
        return "low"
    return "none"


def classify_cause(affected_frac, ndwi_mean, ndvi_delta, flood_thr, claimed_peril=None):
    """Provisional cause evaluation tailored to reported peril and spectral signatures.
    - Flood / Inundation: Flagged anomaly + elevated water/moisture signal (NDWI > flood_thr).
    - Drought / Dryness: Flagged anomaly + persistent canopy dry-down (ΔNDVI < -0.15) with dry NDWI.
    - Hail: Optical multispectral sensors can observe acute defoliation/scarring between adjacent clear passes,
      but cannot confirm hail impact without radar trajectory data. If claimed and acute drop observed, returns
      'hail_suspected (radar verification required)', else 'unknown'.
    - Unknown: When spectral evidence does not clearly distinguish between abiotic stress, disease, or management.
    """
    if affected_frac <= 0:
        return "none", "low"

    peril_str = str(claimed_peril or "").lower()

    if ndwi_mean > flood_thr:
        uncertainty = "low" if ndwi_mean > 1.5 * flood_thr else "medium"
        return "flood", uncertainty
    if "hail" in peril_str and ndvi_delta < -0.10:
        # Acute loss reported as hail: note that satellite indicates physical damage but hail cannot be proved by NDVI alone
        return "hail_suspected", "high"
    if ndvi_delta < -0.15:
        if "drought" in peril_str or "dry" in peril_str:
            return "drought", "low"
        return "drought", "medium"
    return "unknown", "high"


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def read_masked(src, window=None):
    arr = src.read(1, window=window)
    nodata_val = src.nodata if src.nodata is not None else NODATA
    mask = (arr == nodata_val) | (arr == NODATA) | ~np.isfinite(arr)
    return np.ma.masked_where(mask, arr)


def run_grid(cfg, procdir, outdir):
    """Grid analysis for ONE run's rasters; writes into outdir and returns
    a summary dict. Cell geometry and severity come from these rasters
    only — never from a different location or a previous run."""
    os.makedirs(outdir, exist_ok=True)

    srcs = {}
    for name in FILES:
        path = os.path.join(procdir, name)
        if not os.path.exists(path):
            raise FarmAidDataError(
                f"Missing {path}. Run the fetch step for the current "
                f"selection first (the AOI follows the selected geometry).")
        srcs[name] = rasterio.open(path)

    src0 = srcs[FILES[0]]
    for name in FILES[1:]:
        other = srcs[name]
        if (other.width != src0.width or other.height != src0.height
                or other.crs != src0.crs):
            for s in srcs.values():
                s.close()
            raise FarmAidDataError(
                "Rasters are misaligned (different grids/CRS). Re-run the "
                "fetch step for the current selection.")

    mean = read_masked(srcs["baseline_mean.tif"])
    std = read_masked(srcs["baseline_std.tif"])
    ndvi_r = read_masked(srcs["event_ndvi.tif"])
    ndwi_r = read_masked(srcs["event_ndwi.tif"])

    z = anomaly_z(ndvi_r, mean, std)
    threshold = -abs(cfg["ndvi_z_threshold"])
    affected = (z < threshold) & (ndvi_r < mean)
    coverage_global = 100.0 * ndvi_r.count() / ndvi_r.size

    cmap_veg = plt.cm.YlGn.with_extremes(bad="#EBEBEB")
    cmap_aff = plt.cm.YlOrRd.with_extremes(bad="#EBEBEB")

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    im0 = axes[0].imshow(ndvi_r, cmap=cmap_veg, vmin=0.05, vmax=0.8)
    axes[0].set_title("Observation NDVI (composite)")
    plt.colorbar(im0, ax=axes[0], fraction=0.046)
    im1 = axes[1].imshow(affected.astype(float), cmap=cmap_aff, vmin=0, vmax=1)
    axes[1].set_title(f"Negative anomaly (z < {threshold:.1f})")
    plt.colorbar(im1, ax=axes[1], fraction=0.046)
    fig.suptitle("Screening indicators only — not verified crop loss",
                 fontsize=9)
    plt.tight_layout(rect=(0, 0, 1, 0.95))
    quicklook = os.path.join(outdir, "quicklook.png")
    plt.savefig(quicklook, dpi=110)
    plt.close(fig)

    cell = cfg["grid_cell_size_m"]
    xmin, ymin, xmax, ymax = src0.bounds
    ncols = max(int((xmax - xmin) / cell), 1)
    nrows = max(int((ymax - ymin) / cell), 1)
    min_valid = cfg["min_valid_pixels"]
    cut = cfg["severity"]
    flood_thr = cfg["ndwi_flood_threshold"]

    rows = []
    cell_id = 0
    for i in range(nrows):
        y0 = ymax - (i + 1) * cell
        y1 = ymax - i * cell
        for j in range(ncols):
            x0 = xmin + j * cell
            x1 = xmin + (j + 1) * cell
            win = from_bounds(x0, y0, x1, y1, transform=src0.transform)
            if win.width < 1 or win.height < 1:
                continue
            v_ndvi = read_masked(srcs["event_ndvi.tif"], win)
            v_ndwi = read_masked(srcs["event_ndwi.tif"], win)
            v_mean = read_masked(srcs["baseline_mean.tif"], win)
            v_std = read_masked(srcs["baseline_std.tif"], win)
            v_z = anomaly_z(v_ndvi, v_mean, v_std)

            n_valid = int(v_z.count())
            if n_valid < min_valid:
                continue
            total_px = win.width * win.height
            valid_pct = 100.0 * n_valid / total_px

            common_mask = np.ma.getmaskarray(v_z)
            v_ndvi_valid = np.ma.masked_where(common_mask, v_ndvi)
            v_mean_valid = np.ma.masked_where(common_mask, v_mean)
            v_ndwi_valid = np.ma.masked_where(common_mask, v_ndwi)

            v_aff = (v_z < threshold) & (v_ndvi_valid < v_mean_valid)
            affected_frac = (float(np.ma.mean(v_aff)) if v_aff.count()
                             else 0.0)
            ndvi_mean_c = float(np.ma.mean(v_ndvi_valid))
            baseline_mean_c = float(np.ma.mean(v_mean_valid))
            delta = ndvi_mean_c - baseline_mean_c
            z_mean = float(np.ma.mean(v_z))
            ndwi_mean_c = (float(np.ma.mean(v_ndwi_valid)) if v_ndwi_valid.count()
                           else (float(np.ma.mean(v_ndwi)) if v_ndwi.count() else np.nan))

            severity = classify_severity(affected_frac, cut)
            cause, uncertainty = classify_cause(
                affected_frac, ndwi_mean_c, delta, flood_thr)

            cell_id += 1
            rows.append({
                "cell_id": cell_id,
                "affected_pct": round(affected_frac * 100.0, 1),
                "severity": severity,
                "cause": cause,
                "uncertainty": uncertainty,
                "ndvi_mean": round(ndvi_mean_c, 3),
                "ndvi_delta": round(delta, 3),
                "z_score": round(z_mean, 2),
                "ndwi_mean": round(ndwi_mean_c, 3),
                "valid_pct": round(valid_pct, 1),
                "geometry": box(x0, y0, x1, y1),
            })

    gdf = gpd.GeoDataFrame(rows, crs=src0.crs)
    for src in srcs.values():
        src.close()

    if gdf.empty:
        raise FarmAidDataError(
            "No grid cells have enough valid pixels to assess — the imagery "
            "for this window/location is too cloudy or empty.")

    gdf4326 = gdf.to_crs(epsg=4326)
    geojson_path = os.path.join(outdir, "damage.geojson")
    gdf4326.to_file(geojson_path, driver="GeoJSON")

    cents = gpd.GeoSeries(gdf.geometry.centroid,
                          crs=src0.crs).to_crs(epsg=4326)
    csv_path = os.path.join(outdir, "damage_summary.csv")
    csv_df = gdf4326.drop(columns="geometry").copy()
    csv_df["centroid_lon"] = cents.x.round(6).to_numpy()
    csv_df["centroid_lat"] = cents.y.round(6).to_numpy()
    csv_df.to_csv(csv_path, index=False)

    n_aff = int((gdf["severity"] != "none").sum())
    n_flood = int((gdf["cause"] == "flood").sum())
    n_drought = int((gdf["cause"] == "drought").sum())
    n_unknown = int((gdf["cause"] == "unknown").sum())
    mean_valid = float(gdf["valid_pct"].mean())

    sev_counts = {k: int((gdf["severity"] == k).sum())
                  for k in ["none", "low", "moderate", "high"]}
    mean_ndvi_reg = float(gdf["ndvi_mean"].mean())
    mean_delta_reg = float(gdf["ndvi_delta"].mean())
    mean_z_reg = float(gdf["z_score"].mean())

    print(f"\nRaster valid-data coverage : {coverage_global:.1f}%")
    print(f"Cells assessed            : {len(gdf)}")
    print(f"Regional metrics          : mean NDVI {mean_ndvi_reg:.3f} | "
          f"mean dNDVI {mean_delta_reg:+.3f} | mean z {mean_z_reg:+.2f}")
    print(f"Cells with negative anomaly: {n_aff} "
          f"({100.0 * n_aff / len(gdf):.1f}% of grid)")
    print(f"  severity breakdown -> none: {sev_counts['none']} | "
          f"low: {sev_counts['low']} | mod: {sev_counts['moderate']} | "
          f"high: {sev_counts['high']}")
    print(f"  causes -> flood {n_flood} | drought {n_drought} | "
          f"unknown {n_unknown} (hail unsupported)")
    print(f"Mean cell valid coverage   : {mean_valid:.1f}%")
    print(f"Grid outputs -> {outdir}")

    return {
        "n_cells": len(gdf), "n_negative": n_aff,
        "flood": n_flood, "drought": n_drought, "unknown": n_unknown,
        "coverage_pct": round(coverage_global, 1),
        "mean_valid_pct": round(mean_valid, 1),
        "outdir": outdir,
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=os.path.join(ROOT, "config.yaml"))
    ap.add_argument("--processed-dir",
                    help="processed rasters directory (default: "
                         "data/processed/default)")
    ap.add_argument("--out-dir", help="outputs directory")
    args = ap.parse_args()

    cfg = load_config(args.config)
    procdir = args.processed_dir or os.path.join(
        ROOT, cfg["data_dir"], "processed", "default")
    outdir = args.out_dir or os.path.join(ROOT, cfg["out_dir"], "default")
    run_grid(cfg, procdir, outdir)
    print("Stage 2 done. Next: python src/field.py ... | python src/report.py")


if __name__ == "__main__":
    main()