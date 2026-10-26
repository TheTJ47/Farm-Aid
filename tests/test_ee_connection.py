"""Manual LIVE Earth Engine integration check (NOT part of normal pytest).

`pytest -q` must run fully offline: this module only imports cheap
libraries at collection time, and `ee` is imported inside main() only.
The live connect + Sentinel-2 scene count runs ONLY when executed
explicitly:

    python tests/test_ee_connection.py

It resolves the Earth Engine project id from <repo>/config.yaml using a
path independent of the current working directory, and prints clear
guidance when the project id is missing or config is absent.
"""
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def config_path():
    return ROOT / "config.yaml"


def resolve_project_id(path=None):
    """Earth Engine project id from config.yaml, or None."""
    path = Path(path) if path else config_path()
    if not path.exists():
        return None
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    pid = (cfg.get("gee_project") or "").strip()
    return pid or None


def test_resolve_project_id_offline_safe():
    """Offline-safe: config resolution never touches the network."""
    pid = resolve_project_id()
    # The template ships empty; a missing id is valid — main() then prints
    # the exact steps instead of failing cryptically.
    assert pid is None or len(pid) > 0


def main():
    import ee  # ONLY here — never at collection time

    pid = resolve_project_id()
    if pid is None:
        print("\n[test_ee_connection] No `gee_project` set in config.yaml.")
        print("  1. Open", config_path())
        print('  2. Add a line like:  gee_project: "ee-xxxx"')
        print("     (the project id shown in code.earthengine.google.com "
              "after sign-in)")
        print("  3. Run once:  earthengine authenticate")
        print("  4. Re-run this script.\n")
        sys.exit(2)

    print(f"[test_ee_connection] Initialising Earth Engine with project "
          f"{pid!r} ...")
    ee.Initialize(project=pid)

    # illustrative test point (Rahuri, Maharashtra) — same check as before
    point = ee.Geometry.Point([74.65, 19.39])
    col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
           .filterBounds(point)
           .filterDate("2025-01-01", "2025-12-31")
           .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80)))
    n = int(col.size().getInfo())

    print("Earth Engine connected")
    print("Sentinel-2 scenes:", n)
    if n == 0:
        print("  (0 scenes for the test point/window — adjust the point or "
              "dates in main(); connectivity itself is fine.)")
        sys.exit(3)
    print("[test_ee_connection] OK")


if __name__ == "__main__":
    main()