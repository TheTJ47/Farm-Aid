"""FarmAid — persistence for completed assessments.

Saves run summaries, metrics, and artifact pointers to data/saved_assessments/
enabling historical comparison, reload, and review without re-running data fetches.
"""
import datetime as dt
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_SAVED_DIR = os.path.join(ROOT, "data", "saved_assessments")


def get_saved_dir(saved_dir=None):
    target = saved_dir or DEFAULT_SAVED_DIR
    os.makedirs(target, exist_ok=True)
    return target


def save_assessment(run_data, saved_dir=None):
    """Persist a completed run to disk for later comparison/reopening.

    Returns the path to the saved JSON file.
    """
    directory = get_saved_dir(saved_dir)
    run_id = run_data["run_id"]
    save_path = os.path.join(directory, f"{run_id}.json")

    res = run_data.get("res", {})
    record = {
        "run_id": run_id,
        "param_key": run_data.get("param_key"),
        "mode": run_data.get("mode"),
        "event_window": run_data.get("event_window"),
        "baseline_years": run_data.get("baseline_years"),
        "geom_wkt": run_data.get("geom_wkt"),
        "metrics": res.get("metrics", run_data.get("metrics", {})),
        "meta": res.get("meta", run_data.get("meta", {})),
        "grid_summary": run_data.get("grid_summary", {}),
        "outdir": res.get("outdir", run_data.get("outdir", "")),
        "procdir": res.get("procdir", run_data.get("procdir", "")),
        "geojson": res.get("geojson", run_data.get("geojson", "")),
        "metrics_json": res.get("metrics_json", run_data.get("metrics_json", "")),
        "quicklook": res.get("quicklook", run_data.get("quicklook", "")),
        "pdf": res.get("pdf", run_data.get("pdf", "")),
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    with open(save_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, default=str)
    return save_path


def list_saved_assessments(saved_dir=None):
    """List all saved assessment records, newest first."""
    directory = get_saved_dir(saved_dir)
    records = []
    if not os.path.exists(directory):
        return records

    for fname in os.listdir(directory):
        if fname.endswith(".json"):
            try:
                with open(os.path.join(directory, fname), "r", encoding="utf-8") as fh:
                    rec = json.load(fh)
                records.append(rec)
            except Exception:
                continue
    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    return records


def load_saved_assessment(run_id, saved_dir=None):
    """Load a saved assessment by run_id."""
    directory = get_saved_dir(saved_dir)
    path = os.path.join(directory, f"{run_id}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return None
