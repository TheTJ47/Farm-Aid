"""Run identity & staleness helpers — PURE functions, no Streamlit/network,
so their behaviour is unit-testable offline.

Every user-controllable input (mode, geometry, dates, baseline years,
processing knobs) contributes to a deterministic parameter fingerprint.
Any change produces a different fingerprint, which marks previous results
stale — the UI can never present results from a different selection as
if they belonged to the current one.
"""
import datetime
import hashlib
import json
import uuid


def canonical_settings(cfg):
    """Subset of configuration that changes analysis results."""
    keys = ("ndvi_z_threshold", "ndwi_flood_threshold", "grid_cell_size_m",
            "min_valid_pixels", "severity", "max_cloud_pct",
            "aoi_buffer_km", "min_valid_coverage_pct", "aoi_size_km")
    return {k: cfg.get(k) for k in keys}


def param_key(mode, geometry_wkt, dates, baseline_years, settings=None):
    """Deterministic fingerprint of the inputs of one assessment run."""
    payload = json.dumps({
        "mode": mode,
        "geometry": geometry_wkt,
        "dates": list(dates),
        "baseline_years": sorted(baseline_years or []),
        "settings": settings or {},
    }, sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def new_run_id():
    """Unique identifier for one assessment; stamped on every artifact."""
    return ("run_" + datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")
            + "_" + uuid.uuid4().hex[:6])


def results_are_current(run_param_key, current_param_key):
    """True only when the run belongs to exactly these inputs."""
    return bool(run_param_key) and run_param_key == current_param_key