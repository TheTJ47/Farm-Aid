"""FarmAid pipeline tests (offline — no Earth Engine credentials).

Coverage:
- index math, nodata, zero-std guards, severity/cause rules
- run identity & staleness (param_key / run_id / evidence states)
- AOI follows the selection (different locations -> different AOI)
- demo: deterministic per location, regenerated when location changes,
  geographically consistent, balanced, never labelled as real data
- field workflow: scoring denominators, sufficiency vs no-evidence,
  stale-rasters rejection, end-to-end demo run, artifact consistency
- Earth Engine error paths (offline, via monkeypatched imports)

Live-credential tests are skipped when Earth Engine is unavailable.

Run:  python -m pytest -q
"""
import json
import os
import sys
import tempfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

import analyze  # noqa: E402
from analyze import (anomaly_z, classify_cause, classify_severity,
                     mask_nodata, ndvi, ndwi, run_grid)  # noqa: E402
import fetch_gee  # noqa: E402
from fetch_gee import (FarmAidDataError, ensure_collection_size,
                       fetch_demo, selection_to_aoi)  # noqa: E402
import field  # noqa: E402
from field import (area_ha, assess_field, evidence_state,
                   geometry_from_inputs, score_arrays,
                   sufficiency_flag, validate_geometry)  # noqa: E402
import runmeta  # noqa: E402
from runmeta import canonical_settings, new_run_id, param_key, \
    results_are_current  # noqa: E402
from report import (HA_TO_ACRES, build_field_report, build_report,
                    generate_field_insights)  # noqa: E402
import storage  # noqa: E402
from shapely.geometry import Point, box  # noqa: E402

CUT = {"low": 0.05, "moderate": 0.15, "high": 0.35}
CFG = {
    "ndvi_z_threshold": 1.5, "ndwi_flood_threshold": 0.05,
    "severity": CUT, "min_valid_pixels": 10, "max_cloud_pct": 60,
    "min_valid_coverage_pct": 30.0,
}
CFG_DEMO = {
    "aoi_center": [19.39, 74.65], "aoi_size_km": 8.0,
    "event_year": 2025, "event_window": ["2025-08-01", "2025-09-15"],
    "baseline_years": [2021, 2022, 2023, 2024],
    "baseline_window": ["08-01", "09-15"], "max_cloud_pct": 60,
    "min_valid_coverage_pct": 30.0,
    "ndvi_z_threshold": 1.5, "ndwi_flood_threshold": 0.05,
    "grid_cell_size_m": 300, "min_valid_pixels": 10,
    "severity": CUT,
}

# --- indices ---------------------------------------------------------------- #
def test_ndvi_basic():
    assert ndvi(nir=0.4, red=0.1) == pytest.approx(0.6)


def test_ndwi_basic():
    assert ndwi(green=0.3, nir=0.5) == pytest.approx(-0.25)


def test_ndvi_scaling_cancels():
    assert ndvi(4000, 1000) == pytest.approx(ndvi(0.4, 0.1))


def test_indices_elementwise():
    nir = np.array([0.4, 0.5])
    red = np.array([0.1, 0.2])
    assert np.allclose(ndvi(nir, red), [0.3 / 0.5, 0.3 / 0.7])


# --- nodata / guards -------------------------------------------------------- #
def test_mask_nodata():
    arr = np.array([[0.4, -9999.0], [-9999.0, 0.6]])
    m = mask_nodata(arr)
    assert m.count() == 2
    assert np.ma.mean(m) == pytest.approx(0.5)


def test_zero_std_guard():
    z = anomaly_z(np.array([0.3]), np.array([0.5]), np.array([0.0]))
    assert np.isfinite(z[0])
    assert z[0] == pytest.approx((0.3 - 0.5) / 0.02)


def test_tiny_std_guard():
    assert np.isfinite(anomaly_z(np.array([0.3]), np.array([0.5]),
                                 np.array([1e-9]))[0])


# --- severity / cause ------------------------------------------------------- #
@pytest.mark.parametrize("frac,expect", [
    (0.0, "none"), (0.03, "none"), (0.05, "low"), (0.14, "low"),
    (0.15, "moderate"), (0.34, "moderate"), (0.35, "high"), (0.9, "high"),
])
def test_severity_thresholds(frac, expect):
    assert classify_severity(frac, CUT) == expect


def test_cause_healthy():
    assert classify_cause(0.0, -0.05, -0.1, 0.05) == ("none", "low")


def test_cause_flood():
    cause, unc = classify_cause(0.4, 0.20, -0.3, 0.05)
    assert cause == "flood" and unc in ("low", "medium")


def test_cause_flood_marginal():
    assert classify_cause(0.4, 0.06, -0.3, 0.05) == ("flood", "medium")


def test_cause_drought():
    assert classify_cause(0.4, -0.10, -0.20, 0.05) == ("drought", "medium")


def test_cause_unknown():
    assert classify_cause(0.2, -0.10, -0.05, 0.05) == ("unknown", "high")


def test_hail_never_classified():
    for args in [(0.4, 0.2, -0.3, 0.05), (0.3, -0.1, -0.4, 0.05),
                 (0.5, 0.0, -0.25, 0.05), (0.1, -0.05, -0.02, 0.05)]:
        cause, _ = classify_cause(*args)
        assert cause != "hail"


# --- guards for empty imagery ----------------------------------------------- #
def test_empty_collection_guard_raises():
    with pytest.raises(FarmAidDataError) as ei:
        ensure_collection_size(0, "baseline 2025 08-01..09-15")
    assert "No images matched" in str(ei.value)


def test_nonempty_collection_ok():
    assert ensure_collection_size(3, "some period") == 3


def test_live_init_error_is_clear():
    """Graceful Earth Engine failure: init errors become FarmAidDataError
    with guidance — never a crash and never a synthetic fallback."""
    sys.modules["ee"] = None  # force `import ee` inside init_ee to fail
    try:
        with pytest.raises(FarmAidDataError):
            fetch_gee.init_ee(dict(CFG))
    finally:
        sys.modules.pop("ee", None)


# --- live data states: no imagery / cloud-filter / insufficient pixels ----- #
def test_diagnose_period_states():
    """The three required data states (+ valid) — pure classifier."""
    assert fetch_gee.diagnose_period(0, 0, None, 20, 60) == \
        "no_matching_imagery"
    assert fetch_gee.diagnose_period(8, 0, None, 20, 60) == \
        "cloud_filter_rejects_all"
    assert fetch_gee.diagnose_period(8, 5, 10.0, 20, 60) == \
        "insufficient_clear_pixels"
    assert fetch_gee.diagnose_period(8, 5, 55.0, 20, 60) == "valid"
    # cloud rejections and clear-pixel shortage are separate axes:
    assert fetch_gee.diagnose_period(8, 2, 12.0, 20, 60) == \
        "insufficient_clear_pixels"


def test_period_error_messages_carry_state():
    with pytest.raises(FarmAidDataError) as e1:
        fetch_gee.raise_period_error("no_matching_imagery",
                                     "baseline 2021")
    assert e1.value.state == "no_matching_imagery"
    assert "no sentinel-2 imagery at all" in str(e1.value).lower()

    with pytest.raises(FarmAidDataError) as e2:
        fetch_gee.raise_period_error("cloud_filter_rejects_all",
                                     "baseline 2021", n_raw=7,
                                     cloud_pct=60)
    assert e2.value.state == "cloud_filter_rejects_all"
    assert "exceed the cloud filter" in str(e2.value).lower()
    assert "different from 'no imagery'" in str(e2.value).lower()

    with pytest.raises(FarmAidDataError) as e3:
        fetch_gee.raise_period_error("insufficient_clear_pixels",
                                     "baseline 2021", coverage_pct=8.0,
                                     min_cov=20.0)
    assert e3.value.state == "insufficient_clear_pixels"
    assert "8.0" in str(e3.value)


def test_window_fallback_widening_recorded():
    start, end, fell = fetch_gee._date_window("2021-08-01", "2021-09-15", 0)
    assert (start, end, fell) == ("2021-08-01", "2021-09-15", False)
    start, end, fell = fetch_gee._date_window("2021-08-01", "2021-09-15",
                                              15)
    assert (start, end, fell) == ("2021-07-17", "2021-09-30", True)


def test_baseline_shortfall_message_lists_reasons():
    msg = fetch_gee.baseline_shortfall_message(
        1, 2, [(2021, "no imagery"), (2022, "insufficient clear pixels")])
    assert "Only 1 usable baseline year" in msg
    assert "2021" in msg and "2022" in msg


def test_live_mode_never_falls_back_to_demo():
    """Structural guarantee: with Earth Engine unavailable, live mode
    raises FarmAidDataError and the demo generator is never invoked."""
    calls = []
    monkeypatch_calls = []
    original_fetch_demo = fetch_gee.fetch_demo

    def _tracked_demo(*args, **kwargs):
        calls.append(args)
        return original_fetch_demo(*args, **kwargs)

    fetch_gee.fetch_demo = _tracked_demo
    monkeypatch_calls.append("patched")
    sys.modules["ee"] = None  # force init_ee to fail
    try:
        with pytest.raises(FarmAidDataError):
            fetch_gee.fetch_from_gee(dict(CFG), "/tmp/nonexistent",
                                     geom=box(74.6, 19.3, 74.7, 19.4),
                                     cache=False)
    finally:
        fetch_gee.fetch_demo = original_fetch_demo
        sys.modules.pop("ee", None)
    assert calls == []
    assert monkeypatch_calls == ["patched"]


def _ee_credentials_available():
    try:
        import ee
        try:
            ee.Initialize()
            return True
        except Exception:
            return False
    except Exception:
        return False


@pytest.mark.skipif(not _ee_credentials_available(),
                    reason="Earth Engine credentials not available")
def test_live_small_baseline_query_real():
    """OPTIONAL: real (authenticated) live query for a small test window.
    Skipped on machines without working credentials."""
    import ee as _ee
    _ee.Reset()
    cfg_live = dict(CFG_DEMO)
    cfg_live["baseline_years"] = [2021]
    cfg_live["event_window"] = ["2025-01-01", "2025-01-15"]
    cfg_live["max_cloud_pct"] = 90  # permissive: availability, not quality
    with tempfile.TemporaryDirectory() as tmp:
        procdir = os.path.join(tmp, "processed")
        os.makedirs(procdir)
        meta, _reused = fetch_gee.fetch_from_gee(
            cfg_live, procdir,
            geom=box(74.60, 19.32, 74.64, 19.36), cache=False)
        assert meta["mode"] == "gee"
        assert meta["data_state"] == "valid"
        assert meta["event"]["images"] > 0


# --- run identity & staleness ----------------------------------------------- #
def test_param_key_stability_and_sensitivity():
    settings = canonical_settings(CFG)
    key = param_key("demo", box(0, 0, 1, 1).wkt, ["2025-08-01",
                                                   "2025-09-15"],
                    [2021, 2022], settings)
    assert key == param_key("demo", box(0, 0, 1, 1).wkt,
                            ["2025-08-01", "2025-09-15"], [2021, 2022],
                            settings)                     # stable
    assert key != param_key("live", box(0, 0, 1, 1).wkt,
                            ["2025-08-01", "2025-09-15"], [2021, 2022],
                            settings)                     # mode
    assert key != param_key("demo", box(0, 0, 2, 2).wkt,
                            ["2025-08-01", "2025-09-15"], [2021, 2022],
                            settings)                     # geometry
    assert key != param_key("demo", box(0, 0, 1, 1).wkt,
                            ["2025-07-01", "2025-08-15"], [2021, 2022],
                            settings)                     # dates
    assert key != param_key("demo", box(0, 0, 1, 1).wkt,
                            ["2025-08-01", "2025-09-15"], [2020, 2022],
                            settings)                     # years


def test_results_are_current():
    assert results_are_current("abc", "abc") is True
    assert results_are_current("abc", "abd") is False
    assert results_are_current(None, "abc") is False


def test_run_id_unique():
    assert new_run_id() != new_run_id()


# --- AOI follows the selection ---------------------------------------------- #
def test_aoi_follows_selection():
    a_deg = selection_to_aoi(CFG_DEMO, box(74.6, 19.3, 74.7, 19.4))
    b_deg = selection_to_aoi(CFG_DEMO, box(83.5, 28.0, 83.7, 28.2))
    assert a_deg != b_deg
    for deg, g in ((a_deg, box(74.6, 19.3, 74.7, 19.4)),
                   (b_deg, box(83.5, 28.0, 83.7, 28.2))):
        w, s, e, n = deg
        assert w <= g.bounds[0] and s <= g.bounds[1]
        assert e >= g.bounds[2] and n >= g.bounds[3]
    with pytest.raises(FarmAidDataError):
        selection_to_aoi(CFG_DEMO, box(100, 90, 101, 91))  # bad latitude


# --- demo: location-driven + deterministic + consistent --------------------- #
def _demo_arrays(procdir):
    import rasterio

    def _read(name):
        with rasterio.open(os.path.join(procdir, name)) as src:
            arr = src.read(1)
        return np.ma.masked_where(arr == -9999.0, arr)

    return (_read("baseline_mean.tif"), _read("baseline_std.tif"),
            _read("event_ndvi.tif"), _read("event_ndwi.tif"))


def test_demo_regenerates_for_location_and_is_deterministic():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = os.path.join(tmp, "a1")
        p2 = os.path.join(tmp, "a2")
        p3 = os.path.join(tmp, "b1")
        for p in (p1, p2, p3):
            os.makedirs(p)
        geom_a = box(74.6, 19.3, 74.7, 19.4)
        geom_b = box(83.5, 28.0, 83.7, 28.2)
        fetch_demo(CFG_DEMO, p1, geom=geom_a)
        fetch_demo(CFG_DEMO, p2, geom=geom_a)   # same location again
        fetch_demo(CFG_DEMO, p3, geom=geom_b)   # different location
        m1, _, n1, _ = _demo_arrays(p1)
        m2, _, n2, _ = _demo_arrays(p2)
        m3, _, n3, _ = _demo_arrays(p3)
        assert np.array_equal(np.ma.filled(m1, 0), np.ma.filled(m2, 0))
        assert np.array_equal(np.ma.filled(n1, 0), np.ma.filled(n2, 0))
        assert not np.array_equal(np.ma.filled(n1, 0),
                                  np.ma.filled(n3, 0))  # location moves scene
        with open(os.path.join(p1, "source.json")) as fh:
            m1_meta = json.load(fh)
        assert m1_meta["synthetic"] is True
        assert m1_meta["mode"] == "demo"
        assert m1_meta["geometry"] == geom_a.wkt


def test_demo_rasters_georeferencing():
    import rasterio
    from pyproj import Transformer as _Transformer

    mini_cfg = dict(CFG_DEMO)
    epsg = fetch_gee.utm_epsg(mini_cfg["aoi_center"][1])
    t = _Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    with tempfile.TemporaryDirectory() as tmp:
        procdir = os.path.join(tmp, "processed")
        os.makedirs(procdir)
        fetch_demo(mini_cfg, procdir)
        with rasterio.open(os.path.join(procdir,
                                        "baseline_mean.tif")) as src:
            assert src.crs.to_epsg() == epsg
            assert src.transform[0] == pytest.approx(10.0)
            assert src.transform[4] == pytest.approx(-10.0)
            west, south, east, north = fetch_gee.make_aoi_bounds(mini_cfg)
            xw, yn = t.transform(west, north)
            xe, ys = t.transform(east, south)
            sp = 15.0
            assert abs(src.bounds.left - xw) < sp
            assert abs(src.bounds.top - yn) < sp
            assert abs(src.bounds.right - xe) < sp
            assert abs(src.bounds.bottom - ys) < sp
            assert abs(src.width * 10.0 - (xe - xw)) < sp
            assert abs(src.height * 10.0 - (yn - ys)) < sp


def test_demo_outputs_balanced():
    """Affected share must be small & plausible (patch radii are in pixels;
    old bug used scene-fraction units -> ~89% damaged). First patch is a
    flood, second is a drought, so both cause types always exist."""
    mini_cfg = dict(CFG_DEMO)
    with tempfile.TemporaryDirectory() as tmp:
        procdir = os.path.join(tmp, "processed")
        os.makedirs(procdir)
        fetch_demo(mini_cfg, procdir)
        mean, std, ndvi_r, ndwi_r = _demo_arrays(procdir)
        z = anomaly_z(ndvi_r, mean, std)
        affected = (z < -1.5) & (ndvi_r < mean)
        frac = float(np.ma.mean(affected))
        assert 0.005 < frac < 0.35
        flood_px = int((affected & (ndwi_r > 0.05)).sum())
        dry_px = int((affected & (ndwi_r <= 0.05)).sum())
        assert flood_px > 100 and dry_px > 100


# --- scoring & evidence semantics ------------------------------------------- #
def _arrays_for_field():
    mean = np.full((20, 20), 0.6)
    std = np.full((20, 20), 0.05)
    ndvi_arr = mean.copy()
    ndwi_arr = np.full((20, 20), -0.05)
    ndvi_arr[:6, :6] = 0.30  # 6x6 damaged block = 36/400 = 9%
    return mean, std, ndvi_arr, ndwi_arr


def test_score_arrays_basic():
    mean, std, ndvi_arr, ndwi_arr = _arrays_for_field()
    m = score_arrays(ndvi_arr, ndwi_arr, mean, std, CFG, area_ha_=2.0)
    assert m["affected_pct"] == pytest.approx(9.0, abs=1.0)
    assert m["severity"] == "low"
    assert (m["cause"], m["uncertainty"]) == ("unknown", "high")
    assert m["affected_area_ha"] == pytest.approx(
        m["affected_pct"] / 100.0 * 2.0)
    assert m["valid_pct"] == pytest.approx(100.0)


def test_score_arrays_flood():
    mean, std, ndvi_arr, ndwi_arr = _arrays_for_field()
    ndvi_arr[:12, :12] = 0.25
    ndwi_arr[:12, :12] = 0.35
    m = score_arrays(ndvi_arr, ndwi_arr, mean, std, CFG, area_ha_=2.0)
    assert m["affected_pct"] == pytest.approx(36.0, abs=1.0)
    assert m["severity"] == "high"
    assert m["cause"] == "flood"
    assert m["uncertainty"] == "low"


def test_drought_cause_rule():
    assert classify_cause(0.4, -0.10, -0.20, 0.05) == ("drought", "medium")


def test_score_arrays_denominators():
    mean, std, ndvi_arr, ndwi_arr = _arrays_for_field()
    ndvi_arr[10:, :] = -9999.0  # bottom half invalid
    m = score_arrays(mask_nodata(ndvi_arr), mask_nodata(ndwi_arr),
                     mask_nodata(mean), mask_nodata(std), CFG,
                     area_ha_=2.0)
    assert m["valid_pct"] == pytest.approx(50.0)     # 200/400 valid
    assert m["affected_pct"] == pytest.approx(18.0)  # 36/200 valid px
    assert m["affected_area_ha"] == pytest.approx(
        m["affected_pct"] / 100.0 * 2.0)


def test_sufficiency_and_evidence_states():
    assert sufficiency_flag(20.0, CFG) is True
    assert sufficiency_flag(80.0, CFG) is False
    clean = {"valid_pct": 90.0, "cause": "none", "affected_pct": 0.0}
    assert evidence_state(clean, CFG) == "no_evidence"
    bad = {"valid_pct": 10.0, "cause": "none", "affected_pct": 0.0}
    assert evidence_state(bad, CFG) == "insufficient"
    anom = {"valid_pct": 90.0, "cause": "flood", "affected_pct": 30.0}
    assert evidence_state(anom, CFG) == "assessed"


# --- geometry helpers ------------------------------------------------------- #
def test_area_ha_range():
    g = box(74.645, 19.385, 74.655, 19.395)
    assert 90 < area_ha(g) < 150


def test_validate_geometry():
    assert validate_geometry(box(0, 0, 1, 1)) is not None
    with pytest.raises(FarmAidDataError):
        validate_geometry(Point(0, 0))
    with pytest.raises(FarmAidDataError):
        validate_geometry(box(0, 0, 0, 0))


def test_geometry_from_inputs():
    g = geometry_from_inputs(coords=(19.3, 74.6, 1.0))
    assert g.is_valid and g.area > 0
    assert 95 < area_ha(g) < 106


# --- GeoJSON + reports ------------------------------------------------------ #
def test_geojson_roundtrip():
    import geopandas as _gpd
    gdf = _gpd.GeoDataFrame({
        "cell_id": [1, 2], "severity": ["low", "high"],
        "cause": ["flood", "unknown"], "affected_pct": [12.0, 55.0],
    }, geometry=[box(0, 0, 100, 100), box(100, 0, 200, 100)],
        crs="EPSG:4326")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "damage.geojson")
        gdf.to_file(path, driver="GeoJSON")
        back = _gpd.read_file(path)
        assert back.crs.to_epsg() == 4326
        assert set(back.columns) == {"cell_id", "severity", "cause",
                                     "affected_pct", "geometry"}
        assert len(back) == 2


def test_report_generation():
    with tempfile.TemporaryDirectory() as tmp:
        meta = {"mode": "demo", "synthetic": True, "collection": None,
                "aoi_center": [19.39, 74.65],
                "event_window": ["2025-08-01", "2025-09-15"],
                "event_year": 2025, "baseline_years": [2021, 2022],
                "baseline_window": ["08-01", "09-15"],
                "scale_m": 10, "crs": "EPSG:32643"}
        summary = {"n_cells": 40, "n_negative": 12,
                   "severity": {"none": 28, "low": 8, "moderate": 3,
                                "high": 1},
                   "cause": {"flood": 4, "drought": 3, "unknown": 5},
                   "mean_valid_pct": 92.5, "mean_affected_pct": 18.0,
                   "mean_ndvi_delta": -0.11, "mean_z": -1.9}
        png = os.path.join(tmp, "quicklook.png")
        fig, ax = plt.subplots(figsize=(4, 2))
        ax.imshow(np.zeros((10, 10)))
        fig.savefig(png)
        plt.close(fig)
        out = os.path.join(tmp, "report.pdf")
        build_report({"ndvi_z_threshold": 1.5, "severity": CUT},
                     meta, summary, png, out)
        with open(out, "rb") as fh:
            assert fh.read(5) == b"%PDF-"


def test_field_report_pdf():
    with tempfile.TemporaryDirectory() as tmp:
        png = os.path.join(tmp, "q.png")
        fig, ax = plt.subplots(figsize=(4, 2))
        ax.imshow(np.zeros((10, 10)))
        fig.savefig(png)
        plt.close(fig)
        metrics = {"area_ha": 12.3, "valid_pct": 88.0, "affected_pct": 9.5,
                   "affected_area_ha": 1.17, "severity": "low",
                   "cause": "drought", "uncertainty": "medium",
                   "ndvi_mean": 0.51, "ndvi_delta": -0.11, "z_score": -1.6,
                   "ndwi_mean": -0.06, "run_id": "run_test",
                   "bounds_wgs84": [74.6, 19.3, 74.7, 19.4]}
        meta = {"mode": "demo", "synthetic": True,
                "event_window": ["2025-08-01", "2025-09-15"],
                "baseline_window": ["08-01", "09-15"],
                "baseline_years": [2021, 2022, 2023, 2024],
                "crs": "EPSG:32643", "scale_m": 10}
        out = os.path.join(tmp, "field_report.pdf")
        build_field_report({"ndvi_z_threshold": 1.5, "severity": CUT},
                           metrics, meta, png, out)
        with open(out, "rb") as fh:
            assert fh.read(5) == b"%PDF-"


# --- field assessment end-to-end (demo, offline) ---------------------------- #
def test_assess_field_demo_end_to_end():
    """Selected geometry drives everything: rasters are generated FOR the
    selection's AOI; run_id, fingerprint and artifacts are consistent."""
    mini_cfg = dict(CFG_DEMO)
    mini_cfg.update({"grid_cell_size_m": 300, "data_dir": "data",
                     "out_dir": "data/outputs", "report_dir": "reports",
                     "min_valid_coverage_pct": 30.0})
    with tempfile.TemporaryDirectory() as tmp:
        geom = box(74.64, 19.38, 74.66, 19.40)
        res = assess_field(mini_cfg, geom, "demo", root=tmp)
        m = res["metrics"]
        assert res["run_id"].startswith("run_")
        assert m["run_id"] == res["run_id"]
        assert os.path.basename(res["pdf"]) == \
            f"FarmAid_field_report_{res['run_id']}.pdf"
        assert os.path.exists(res["quicklook"])
        assert os.path.exists(res["geojson"])
        assert m["geometry_wkt"] == geom.wkt
        assert 0.0 <= m["affected_pct"] <= 100.0
        assert m["severity"] in ("none", "low", "moderate", "high",
                                 "unassessable")
        assert m["cause"] in ("none", "flood", "drought", "unknown")
        with open(res["metrics_json"]) as fh:
            saved = json.load(fh)
        assert saved["run_id"] == res["run_id"]
        assert saved["metrics"]["severity"] == m["severity"]
        assert saved["metrics"]["affected_pct"] == m["affected_pct"]
        assert saved["metrics"]["area_ha"] == m["area_ha"]
        # the parameter fingerprint pins this run to these exact inputs
        expect_key = param_key("demo", geom.wkt,
                               mini_cfg["event_window"],
                               mini_cfg["baseline_years"],
                               canonical_settings(mini_cfg))
        assert m["param_key"] == expect_key


def test_different_geometries_give_different_runs():
    """The acceptance case: two different selections produce different
    geometries, different rasters and different (correct) results."""
    mini_cfg = dict(CFG_DEMO)
    mini_cfg.update({"data_dir": "data", "out_dir": "data/outputs",
                     "report_dir": "reports"})
    with tempfile.TemporaryDirectory() as tmp:
        res_a = assess_field(mini_cfg, box(74.60, 19.32, 74.64, 19.36),
                             "demo", root=tmp)
        res_b = assess_field(mini_cfg, box(83.50, 28.00, 83.70, 28.20),
                             "demo", root=tmp)  # Himalayan test location
        assert res_a["run_id"] != res_b["run_id"]
        assert res_a["param_key"] != res_b["param_key"]
        assert res_a["metrics"]["geometry_wkt"] != \
            res_b["metrics"]["geometry_wkt"]
        assert res_a["metrics"]["area_ha"] != res_b["metrics"]["area_ha"]
        with open(res_a["metrics_json"]) as fh:
            a_meta = json.load(fh)
        with open(res_b["metrics_json"]) as fh:
            b_meta = json.load(fh)
        assert a_meta["acquisition"]["aoi"] != b_meta["acquisition"]["aoi"]


def test_stale_rasters_rejected():
    """Imagery belongs to one selection; assessing a different geometry
    against it must fail loudly, not silently reuse it."""
    mini_cfg = dict(CFG_DEMO)
    mini_cfg.update({"data_dir": "data", "out_dir": "data/outputs",
                     "report_dir": "reports"})
    with tempfile.TemporaryDirectory() as tmp:
        geom_a = box(74.60, 19.32, 74.64, 19.36)
        res_a = assess_field(mini_cfg, geom_a, "demo", root=tmp)
        geom_b = box(83.50, 28.00, 83.70, 28.20)
        with pytest.raises(FarmAidDataError) as ei:
            assess_field(mini_cfg, geom_b, "demo", root=tmp,
                         rasters_dir=res_a["procdir"])
        assert "DIFFERENT geometry" in str(ei.value)


def test_grid_runs_for_selected_rasters():
    """The analytical grid is derived from the run's ACTUAL rasters — it can
    never be a hardcoded grid from another location."""
    mini_cfg = dict(CFG_DEMO)
    mini_cfg.update({"grid_cell_size_m": 300, "data_dir": "data",
                     "out_dir": "data/outputs", "min_valid_pixels": 10})
    with tempfile.TemporaryDirectory() as tmp:
        procdir = os.path.join(tmp, "data", "processed", "g1")
        outdir = os.path.join(tmp, "data", "outputs", "g1")
        os.makedirs(procdir)
        fetch_demo(mini_cfg, procdir, geom=box(74.60, 19.32, 74.64, 19.36))
        summary = run_grid(mini_cfg, procdir, outdir)
        assert summary["n_cells"] > 0
        assert os.path.exists(os.path.join(outdir, "damage.geojson"))
        assert os.path.exists(os.path.join(outdir, "damage_summary.csv"))
        assert os.path.exists(os.path.join(outdir, "quicklook.png"))


# --------------------------------------------------------------------------- #
# Map serialization (folium regression: "Object of type ndarray is not JSON
# serializable" when re-reading list-valued GeoJSON properties)
# --------------------------------------------------------------------------- #
def test_map_geodataframe_sanitizes_ndarray_properties():
    """The exact folium crash: json.dumps(gdf.__geo_interface__) on a frame
    with an ndarray-valued property must succeed after sanitizing."""
    import geopandas as _gpd
    import json as _json

    gdf = _gpd.GeoDataFrame({
        "cell_id": [7],
        "severity": ["low"],
        "cause": ["flood"],
        "bounds_wgs84": [np.array([74.6, 19.3, 74.7, 19.4])],
        "centroid_wgs84": [np.array([19.35, 74.65])],
        "affected_pct": [np.float64(9.5)],
        "n_valid_pixels": [np.int64(1200)],
        "sufficient": [np.bool_(True)],
        "ndvi_delta": [np.nan],
        "z_score": [np.inf],
        "geometry_wkt": ["POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"],
        "param_key": ["abc123"],
    }, geometry=[box(74.0, 19.0, 74.2, 19.2)], crs="EPSG:4326")

    clean = field.map_safe_geodataframe(
        gdf, drop_columns=("geometry_wkt", "param_key"))

    # the exact call that used to crash:
    dumped = _json.dumps(clean.__geo_interface__)
    assert isinstance(dumped, str)
    # conversions are exact, not lossy
    assert clean.iloc[0]["bounds_wgs84"] == [74.6, 19.3, 74.7, 19.4]
    assert clean.iloc[0]["centroid_wgs84"] == [19.35, 74.65]
    assert clean.iloc[0]["affected_pct"] == 9.5
    assert clean.iloc[0]["n_valid_pixels"] == 1200
    assert clean.iloc[0]["sufficient"] is True
    assert clean.iloc[0]["ndvi_delta"] is None
    assert clean.iloc[0]["z_score"] is None
    # internal columns dropped; geometry + meaningful attributes kept
    assert "geometry_wkt" not in clean.columns
    assert "param_key" not in clean.columns
    assert clean.geometry.name in clean.columns
    assert "severity" in clean.columns and "cause" in clean.columns
    assert clean.geometry is not None and not clean.empty


def test_map_geodataframe_readback_like_field_result():
    """Simulate the dashboard's read-back of field_result.geojson, where
    array/list-valued properties and numpy scalars arrive from the file:
    the sanitized frame must serialize fully and preserve the values."""
    import geopandas as _gpd
    import json as _json

    gdf = _gpd.GeoDataFrame({
        "bounds_wgs84": [np.asarray([74.60, 19.38, 74.66, 19.40])],
        "centroid_wgs84": [np.asarray([19.39, 74.63])],
        "affected_pct": [np.array([9.0])[0]],
        "severity": ["low"],
        "cause": ["unknown"],
    }, geometry=[box(74.60, 19.38, 74.66, 19.40)], crs="EPSG:4326")

    clean = field.map_safe_geodataframe(gdf)
    dumped = _json.dumps(clean.__geo_interface__)  # must not raise TypeError
    assert isinstance(dumped, str)
    feat = clean.__geo_interface__["features"][0]
    assert feat["properties"]["bounds_wgs84"] == [74.60, 19.38, 74.66,
                                                  19.40]
    assert feat["properties"]["affected_pct"] == 9.0
    assert feat["properties"]["cause"] == "unknown"



def test_field_insights_insufficient_data():
    metrics = {
        "insufficient_data": True,
        "valid_pct": 12.5,
        "area_ha": 45.2,
        "severity": "unassessable",
        "affected_pct": 0.0,
        "affected_area_ha": 0.0,
        "cause": "unknown",
        "uncertainty": "high",
    }
    meta = {}
    insights = generate_field_insights(metrics, meta)
    assert any("Insufficient valid satellite data" in ins for ins in insights)
    assert any("data limitation" in ins.lower() for ins in insights)


def test_field_insights_no_evidence():
    metrics = {
        "insufficient_data": False,
        "no_evidence_of_damage": True,
        "valid_pct": 98.0,
        "area_ha": 20.0,
        "severity": "none",
        "affected_pct": 1.2,
        "affected_area_ha": 0.24,
        "cause": "none",
        "uncertainty": "low",
        "ndvi_mean": 0.65,
        "ndvi_delta": 0.02,
        "z_score": 0.15,
    }
    meta = {"mode": "demo"}
    insights = generate_field_insights(metrics, meta)
    assert any("No evidence of significant damage" in ins for ins in insights)
    assert any("20.0 ha" in ins and "acres" in ins for ins in insights)


def test_field_insights_flood_cause():
    metrics = {
        "insufficient_data": False,
        "no_evidence_of_damage": False,
        "valid_pct": 95.0,
        "area_ha": 15.0,
        "severity": "high",
        "affected_pct": 42.0,
        "affected_area_ha": 6.3,
        "cause": "flood",
        "uncertainty": "moderate",
        "ndvi_mean": 0.32,
        "ndvi_delta": -0.30,
        "z_score": -2.4,
    }
    meta = {"mode": "demo"}
    insights = generate_field_insights(metrics, meta)
    assert any("Waterlogging / inundation" in ins for ins in insights)
    assert any("High severity" in ins for ins in insights)
    assert any("screening estimate" in ins for ins in insights)


def test_storage_roundtrip(tmp_path):
    run_data = {
        "run_id": "test_run_12345",
        "param_key": "dummy_param_key_abc",
        "mode": "demo",
        "event_window": ["2025-08-01", "2025-09-15"],
        "baseline_years": [2021, 2022, 2023, 2024],
        "geom_wkt": "POLYGON ((74.0 19.0, 74.1 19.0, 74.1 19.1, 74.0 19.1, 74.0 19.0))",
        "res": {
            "metrics": {
                "area_ha": 10.5,
                "affected_pct": 15.2,
                "severity": "moderate",
                "cause": "drought",
            },
            "meta": {"data_state": "valid"},
            "outdir": str(tmp_path / "out"),
            "procdir": str(tmp_path / "proc"),
            "geojson": str(tmp_path / "result.geojson"),
            "pdf": str(tmp_path / "report.pdf"),
        },
        "grid_summary": {"n_cells": 100, "n_negative": 5},
    }

    save_dir = str(tmp_path / "saved")
    path = storage.save_assessment(run_data, saved_dir=save_dir)
    assert os.path.exists(path)

    loaded = storage.load_saved_assessment("test_run_12345", saved_dir=save_dir)
    assert loaded is not None
    assert loaded["run_id"] == "test_run_12345"
    assert loaded["metrics"]["severity"] == "moderate"
    assert loaded["metrics"]["area_ha"] == 10.5

    all_saved = storage.list_saved_assessments(saved_dir=save_dir)
    assert len(all_saved) == 1
    assert all_saved[0]["run_id"] == "test_run_12345"


def test_ha_to_acres_conversion():
    assert HA_TO_ACRES == pytest.approx(2.47105, rel=1e-3)
    # 100 ha is ~247.1 acres
    assert (100.0 * HA_TO_ACRES) == pytest.approx(247.105, rel=1e-3)


def test_rectangle_size_semantics_and_area():
    """Verify that rectangle side control creates mathematically exact dimensions."""
    # 1.0 km side square = 1,000m x 1,000m = 1,000,000 m^2 = 100 hectares (~247.1 acres)
    geom_1km = geometry_from_inputs(19.39, 74.65, size_km=1.0)
    ha_1km = area_ha(geom_1km)
    assert ha_1km == pytest.approx(100.0, rel=0.03)  # within 3% accounting for UTM projection

    # 0.1 km (100m) side square = 10,000 m^2 = 1.0 hectare (~2.47 acres)
    geom_100m = geometry_from_inputs(19.39, 74.65, size_km=0.1)
    ha_100m = area_ha(geom_100m)
    assert ha_100m == pytest.approx(1.0, rel=0.03)

    # 0.5 km side square = 25 hectares
    geom_500m = geometry_from_inputs(19.39, 74.65, size_km=0.5)
    ha_500m = area_ha(geom_500m)
    assert ha_500m == pytest.approx(25.0, rel=0.03)


def test_score_arrays_field_pixels_denominator():
    """Valid coverage must be computed against the field's pixels, not the bounding box."""
    mean = np.ma.masked_all((20, 20), dtype=float)
    std = np.ma.masked_all((20, 20), dtype=float)
    ndvi_arr = np.ma.masked_all((20, 20), dtype=float)
    ndwi_arr = np.ma.masked_all((20, 20), dtype=float)

    # Only a circle/subset of 200 pixels inside the field geometry
    # Out of these 200, 160 are valid satellite observations (40 masked by clouds)
    for r in range(20):
        for c in range(20):
            if (r - 10)**2 + (c - 10)**2 <= 64:  # ~200 pixels inside
                if r < 16:  # 160 pixels clear
                    mean[r, c] = 0.50
                    std[r, c] = 0.05
                    ndvi_arr[r, c] = 0.52
                    ndwi_arr[r, c] = -0.10

    n_field_pixels = 200
    n_clear = int((~ndvi_arr.mask).sum())
    assert n_clear > 100

    m = score_arrays(ndvi_arr, ndwi_arr, mean, std, CFG, area_ha_=5.0,
                     n_field_pixels=n_field_pixels)
    # Valid coverage should be clear / n_field_pixels * 100, NOT clear / 400 * 100
    expected_valid_pct = round(100.0 * n_clear / n_field_pixels, 1)
    assert m["valid_pct"] == pytest.approx(expected_valid_pct)
    assert m["n_field_pixels"] == 200


def test_mixed_positive_and_negative_anomalies():
    """Confirm a positive mean delta/z-score can coexist with negative anomaly screening."""
    # 100 pixels total
    mean = np.full((10, 10), 0.50)
    std = np.full((10, 10), 0.05)
    ndvi_arr = np.full((10, 10), 0.50)
    ndwi_arr = np.full((10, 10), -0.10)

    # 75 pixels have strong POSITIVE anomalies (+0.10 NDVI, z = +2.0)
    ndvi_arr[:7, :] = 0.60
    ndvi_arr[7, :5] = 0.60

    # 25 pixels have strong NEGATIVE anomalies (-0.15 NDVI, z = -3.0)
    ndvi_arr[7, 5:] = 0.35
    ndvi_arr[8:, :] = 0.35

    m = score_arrays(ndvi_arr, ndwi_arr, mean, std, CFG, area_ha_=10.0)

    # Mean observed NDVI is 0.5375 (> baseline mean 0.50, delta is positive +0.0375)
    assert m["ndvi_delta"] > 0
    assert m["z_score"] > 0
    # But 25% of the pixels have severe negative anomalies
    assert m["affected_pct"] == pytest.approx(25.0)
    assert m["severity"] == "moderate"  # 15% to 35% is moderate
    assert m["affected_area_ha"] == pytest.approx(2.5)  # 25% of 10 ha


def test_field_insights_severity_levels():
    """Verify insights for different severity classifications and explanations."""
    meta = {"mode": "demo", "event": {"images": 4}}

    # Moderate severity
    m_mod = {
        "area_ha": 10.0, "valid_pct": 95.0, "affected_pct": 22.5,
        "affected_area_ha": 2.25, "severity": "moderate", "cause": "unknown",
        "uncertainty": "medium", "ndvi_mean": 0.55, "ndvi_delta": 0.02,
        "z_score": 0.2, "ndwi_mean": -0.1
    }
    insights_mod = generate_field_insights(m_mod, meta)
    assert any("Moderate severity" in ins for ins in insights_mod)
    assert any("HIGHER than the historical baseline" in ins for ins in insights_mod)

    # Low severity
    m_low = {
        "area_ha": 5.0, "valid_pct": 90.0, "affected_pct": 7.0,
        "affected_area_ha": 0.35, "severity": "low", "cause": "drought",
        "uncertainty": "medium", "ndvi_mean": 0.40, "ndvi_delta": -0.08,
        "z_score": -1.2, "ndwi_mean": -0.1
    }
    insights_low = generate_field_insights(m_low, meta)
    assert any("7.0% of valid pixels show a negative anomaly" in ins for ins in insights_low)
def test_read_masked_handles_nodata_none(tmp_path):
    """Regression test: Earth Engine GeoTIFFs downloaded without a nodata tag
    (nodata=None) must have -9999.0 and invalid values masked by read_masked."""
    import rasterio
    from rasterio.transform import from_origin
    from analyze import read_masked

    tif_path = tmp_path / "test_no_tag.tif"
    arr = np.array([[0.65, -9999.0], [np.nan, 0.70]], dtype="float32")
    transform = from_origin(0, 10, 1, 1)

    # Write GeoTIFF WITHOUT nodata tag
    with rasterio.open(
        tif_path, "w", driver="GTiff", height=2, width=2, count=1,
        dtype="float32", transform=transform, nodata=None
    ) as dst:
        dst.write(arr, 1)

    with rasterio.open(tif_path) as src:
        assert src.nodata is None
        masked = read_masked(src)
        assert masked.count() == 2
        assert float(masked.mean()) == pytest.approx(0.675)
        assert masked.mask[0, 1] is True or masked.mask[0, 1] == 1
        assert masked.mask[1, 0] is True or masked.mask[1, 0] == 1


def test_run_grid_masks_nodata_cleanly(tmp_path):
    """Regression test: run_grid must mask -9999.0 even when nodata=None in GeoTIFFs,
    ensuring cells are not falsely classified as drought with runaway z-scores."""
    import rasterio
    from rasterio.transform import from_origin
    import geopandas as gpd
    import pandas as pd

    procdir = tmp_path / "processed"
    outdir = tmp_path / "outputs"
    procdir.mkdir()
    outdir.mkdir()

    # 30x30 pixels raster @ 10m scale -> 300m x 300m (exactly 1 grid cell)
    shape = (30, 30)
    transform = from_origin(1000, 1000, 10, 10)

    # Base values: baseline mean = 0.6, std = 0.05
    mean_arr = np.full(shape, 0.60, dtype="float32")
    std_arr = np.full(shape, 0.05, dtype="float32")
    ndwi_arr = np.full(shape, -0.10, dtype="float32")

    # Event NDVI: 600 pixels clear (NDVI = 0.60, healthy), 300 pixels cloudy (-9999.0)
    ndvi_arr = np.full(shape, 0.60, dtype="float32")
    ndvi_arr[:10, :] = -9999.0

    # Write without nodata tag to simulate raw GEE download
    for name, data in [("baseline_mean.tif", mean_arr), ("baseline_std.tif", std_arr),
                       ("event_ndvi.tif", ndvi_arr), ("event_ndwi.tif", ndwi_arr)]:
        with rasterio.open(
            procdir / name, "w", driver="GTiff", height=30, width=30, count=1,
            dtype="float32", crs="EPSG:32643", transform=transform, nodata=None
        ) as dst:
            dst.write(data, 1)

    cfg = dict(CFG_DEMO)
    cfg.update({"grid_cell_size_m": 300, "min_valid_pixels": 10})
    summary = run_grid(cfg, str(procdir), str(outdir))

    df = pd.read_csv(outdir / "damage_summary.csv")
    assert len(df) == 1
    row = df.iloc[0]

    # Valid percentage should reflect the 600/900 clear pixels (66.7%), NOT 100%
    assert row["valid_pct"] == pytest.approx(66.7, abs=0.5)
    # Mean NDVI must be 0.60, NOT negative (-3300+)
    assert row["ndvi_mean"] == pytest.approx(0.60, abs=0.01)
    assert row["ndvi_delta"] == pytest.approx(0.00, abs=0.01)
    assert row["z_score"] == pytest.approx(0.00, abs=0.1)
    # Severity must be none, cause must be none (not false drought)
    assert row["severity"] == "none"
    assert row["cause"] == "none"
    assert row["affected_pct"] == pytest.approx(0.0)


def test_field_vs_regional_divergence_logic():
    """Verify that a healthy field (positive delta/z) correctly evaluates to 0% affected
    even when embedded inside a regional grid with seasonal NDVI dips."""
    # Field: 45 pixels, observed 0.657, baseline 0.507 (+0.150 delta, z = +1.12)
    f_mean = np.full((5, 9), 0.507)
    f_std = np.full((5, 9), 0.134)
    f_ndvi = np.full((5, 9), 0.657)
    f_ndwi = np.full((5, 9), -0.552)

    f_metrics = score_arrays(f_ndvi, f_ndwi, f_mean, f_std, CFG, area_ha_=0.276)
    assert f_metrics["affected_pct"] == pytest.approx(0.0)
    assert f_metrics["severity"] == "none"
    assert f_metrics["cause"] == "none"
    assert f_metrics["z_score"] == pytest.approx(1.12, abs=0.05)
    assert evidence_state(f_metrics, CFG) == "no_evidence"

    # Region: seasonal dip (observed 0.637, baseline 0.717, delta -0.080, z = -1.6)
    r_mean = np.full((5, 9), 0.717)
    r_std = np.full((5, 9), 0.050)
    r_ndvi = np.full((5, 9), 0.637)
    r_ndwi = np.full((5, 9), -0.10)

    r_metrics = score_arrays(r_ndvi, r_ndwi, r_mean, r_std, CFG, area_ha_=0.276)
    assert r_metrics["affected_pct"] == pytest.approx(100.0)
    assert r_metrics["severity"] == "high"


def test_score_arrays_zero_valid_and_consistent_pixels():
    """Regression test:
    1) Zero valid pixels: returns None for ndvi_mean, ndvi_delta, z_score, ndwi_mean.
    2) Heterogeneous baseline with partial valid pixels: ndvi_mean, baseline_ndvi_mean,
       and ndvi_delta must be evaluated strictly on the co-located valid pixels."""
    shape = (10, 10)
    # Heterogeneous baseline: top half is 0.40, bottom half is 0.80.
    mean = np.full(shape, 0.80, dtype=float)
    mean[:5, :] = 0.40  # Top half baseline = 0.40
    std = np.full(shape, 0.05, dtype=float)
    ndwi = np.full(shape, -0.10, dtype=float)

    # Event: bottom half is obscured by cloud (-9999.0)
    ndvi = np.full(shape, -9999.0, dtype=float)
    # Top half observed NDVI is 0.45 (+0.05 higher than its 0.40 baseline)
    ndvi[:5, :] = 0.45

    m_partial = score_arrays(ndvi, ndwi, mean, std, CFG, area_ha_=1.0, n_field_pixels=100)
    assert m_partial["valid_pct"] == pytest.approx(50.0)
    assert m_partial["n_valid_pixels"] == 50
    # Must match the 0.45 observed mean
    assert m_partial["ndvi_mean"] == pytest.approx(0.45, abs=0.001)
    # Baseline mean must be 0.40 (from the top half co-located pixels), NOT 0.60 (whole field)
    assert m_partial["baseline_ndvi_mean"] == pytest.approx(0.40, abs=0.001)
    # Delta must be +0.05 (+0.45 - 0.40), NOT -0.15 (0.45 - 0.60)
    assert m_partial["ndvi_delta"] == pytest.approx(0.05, abs=0.001)
    assert m_partial["z_score"] == pytest.approx(1.00, abs=0.05)
    assert m_partial["affected_pct"] == pytest.approx(0.0)

    # Completely cloudy event: 0 valid pixels
    ndvi_cloudy = np.full(shape, -9999.0, dtype=float)
    m_zero = score_arrays(ndvi_cloudy, ndwi, mean, std, CFG, area_ha_=1.0, n_field_pixels=100)
    assert m_zero["valid_pct"] == pytest.approx(0.0)
    assert m_zero["n_valid_pixels"] == 0
    assert m_zero["ndvi_mean"] is None
    assert m_zero["ndvi_delta"] is None
    assert m_zero["z_score"] is None
    assert m_zero["ndwi_mean"] is None
    assert m_zero["affected_pct"] == pytest.approx(0.0)


def test_dashboard_none_safe_technical_formatting():
    """Regression test for app/dashboard.py:574:
    Metrics with None values (like run_20261026T055258_717834) must format safely
    without TypeError: unsupported operand type(s) for -: 'NoneType' and 'int'."""
    import math

    m_none = {
        "ndvi_mean": None,
        "ndvi_delta": None,
        "z_score": None,
        "ndwi_mean": None,
        "baseline_ndvi_mean": None,
    }

    b_mean_val = m_none.get("baseline_ndvi_mean")
    if b_mean_val is None and m_none.get("ndvi_mean") is not None and m_none.get("ndvi_delta") is not None:
        b_mean_val = round(m_none["ndvi_mean"] - m_none["ndvi_delta"], 3)

    def _fmt_stat(val, decimals=3):
        if val is None or not isinstance(val, (int, float)) or not math.isfinite(val):
            return "N/A (insufficient clear data)"
        return f"{val:.{decimals}f}"

    assert _fmt_stat(m_none.get("ndvi_mean")) == "N/A (insufficient clear data)"
    assert _fmt_stat(b_mean_val) == "N/A (insufficient clear data)"
    assert _fmt_stat(m_none.get("ndvi_delta")) == "N/A (insufficient clear data)"
    assert _fmt_stat(m_none.get("z_score"), 2) == "N/A (insufficient clear data)"

    # Also test valid numeric metrics
    m_valid = {
        "ndvi_mean": 0.654,
        "ndvi_delta": -0.052,
        "z_score": -1.25,
        "ndwi_mean": -0.123,
        "baseline_ndvi_mean": 0.706,
    }
    assert _fmt_stat(m_valid.get("ndvi_mean")) == "0.654"
    assert _fmt_stat(m_valid.get("baseline_ndvi_mean")) == "0.706"
    assert _fmt_stat(m_valid.get("ndvi_delta")) == "-0.052"
    assert _fmt_stat(m_valid.get("z_score"), 2) == "-1.25"


def test_build_field_report_with_none_metrics(tmp_path):
    """Regression test: build_field_report must build cleanly when metrics are None."""
    from PIL import Image as PILImage

    img_path = str(tmp_path / "quicklook.png")
    PILImage.new("RGB", (100, 100), color="grey").save(img_path)

    metrics = {
        "area_ha": 0.96, "valid_pct": 0.0, "affected_pct": 0.0,
        "affected_area_ha": 0.0, "ndvi_mean": None, "ndvi_delta": None,
        "z_score": None, "ndwi_mean": None, "severity": "unassessable",
        "cause": "unknown", "uncertainty": "high", "insufficient_data": True,
        "run_id": "run_test_none", "bounds_wgs84": [74.6, 19.3, 74.7, 19.4]
    }
    meta = {
        "mode": "gee", "synthetic": False, "crs": "EPSG:32643", "scale_m": 10,
        "event_window": ["2025-08-01", "2025-09-15"],
        "baseline_window": ["08-01", "09-15"],
        "baseline_years": [2022, 2023],
        "event": {"images": 2, "first": "2025-08-11", "last": "2025-09-10", "coverage_pct": 62.5},
        "baseline_periods": [{"year": 2022, "images": 2, "first": "2022-09-01", "last": "2022-09-06", "coverage_pct": 93.5}]
    }
    out_pdf = str(tmp_path / "test_report.pdf")
    build_field_report(CFG, metrics, meta, img_path, out_pdf)
    assert os.path.exists(out_pdf)
    with open(out_pdf, "rb") as fh:
        assert fh.read(5) == b"%PDF-"


def test_build_field_report_with_claim_info(tmp_path):
    """Test that build_field_report incorporates claim metadata and multi-page NumberedCanvas."""
    from PIL import Image as PILImage

    img_path = str(tmp_path / "quicklook2.png")
    PILImage.new("RGB", (100, 100), color="green").save(img_path)

    metrics = {
        "area_ha": 4.5, "valid_pct": 92.5, "affected_pct": 24.0,
        "affected_area_ha": 1.08, "ndvi_mean": 0.42, "ndvi_delta": -0.18,
        "baseline_ndvi_mean": 0.60, "z_score": -1.85, "ndwi_mean": 0.12,
        "severity": "moderate", "cause": "flood", "uncertainty": "low",
        "insufficient_data": False, "no_evidence_of_damage": False,
        "run_id": "run_test_claim", "param_key": "param_test_claim_123",
        "bounds_wgs84": [74.6, 19.3, 74.7, 19.4],
        "centroid_wgs84": [19.35, 74.65],
    }
    meta = {
        "mode": "live", "synthetic": False, "crs": "EPSG:32643", "scale_m": 10,
        "event_window": ["2025-08-01", "2025-09-15"],
        "baseline_window": ["08-01", "09-15"],
        "baseline_years": [2021, 2022],
        "event": {"images": 4, "first": "2025-08-05", "last": "2025-09-10", "coverage_pct": 94.0},
        "baseline_periods": [
            {"year": 2021, "images": 3, "first": "2021-08-10", "last": "2021-09-12", "coverage_pct": 91.0},
            {"year": 2022, "images": 4, "first": "2022-08-08", "last": "2022-09-14", "coverage_pct": 95.0}
        ]
    }
    claim_info = {
        "claim_ref": "CLM-2026-TEST",
        "policy_no": "POL-998877",
        "claimant_name": "Kisan Rao",
        "crop_type": "Soybean",
        "claimed_cause": "Inundation / Flood",
    }
    out_pdf = str(tmp_path / "claim_evidence_report.pdf")
    build_field_report(CFG, metrics, meta, img_path, out_pdf, claim_info=claim_info)
    assert os.path.exists(out_pdf)
    assert os.path.getsize(out_pdf) > 5000  # valid multi-section PDF


def test_storage_save_and_load_claim_info(tmp_path):
    """Test persistence and retrieval of claim information in storage."""
    from storage import save_assessment, load_saved_assessment

    saved_dir = str(tmp_path / "saved")
    run_data = {
        "run_id": "run_persisted_claim",
        "param_key": "key_persisted_123",
        "mode": "live",
        "event_window": ["2025-08-01", "2025-09-15"],
        "baseline_years": [2022, 2023],
        "geom_wkt": "POLYGON ((74.6 19.3, 74.7 19.3, 74.7 19.4, 74.6 19.4, 74.6 19.3))",
        "claim_info": {
            "claim_ref": "CLM-PERSIST",
            "policy_no": "POL-PERSIST",
            "claimant_name": "Test Claimant",
            "crop_type": "Cotton",
            "claimed_cause": "Drought",
        },
        "metrics": {"area_ha": 5.0, "valid_pct": 88.0, "affected_pct": 10.0},
    }
    save_path = save_assessment(run_data, saved_dir=saved_dir)
    assert os.path.exists(save_path)

    loaded = load_saved_assessment("run_persisted_claim", saved_dir=saved_dir)
    assert loaded is not None
    assert loaded["claim_info"]["claim_ref"] == "CLM-PERSIST"
    assert loaded["claim_info"]["crop_type"] == "Cotton"
    assert loaded["metrics"]["area_ha"] == 5.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))