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
        "workflow_mode": run_data.get("workflow_mode"),
        "incident_date": run_data.get("incident_date"),
        "claimed_peril": run_data.get("claimed_peril"),
        "event_window": run_data.get("event_window"),
        "baseline_years": run_data.get("baseline_years"),
        "geom_wkt": run_data.get("geom_wkt"),
        "discovery": run_data.get("discovery"),
        "metrics": res.get("metrics", run_data.get("metrics", {})),
        "meta": res.get("meta", run_data.get("meta", {})),
        "grid_summary": run_data.get("grid_summary", {}),
        "outdir": res.get("outdir", run_data.get("outdir", "")),
        "procdir": res.get("procdir", run_data.get("procdir", "")),
        "geojson": res.get("geojson", run_data.get("geojson", "")),
        "metrics_json": res.get("metrics_json", run_data.get("metrics_json", "")),
        "quicklook": res.get("quicklook", run_data.get("quicklook", "")),
        "pdf": res.get("pdf", run_data.get("pdf", "")),
        "claim_info": run_data.get("claim_info", res.get("claim_info", {})),
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
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def load_active_assessment(run_id, saved_dir=None):
    """Load and structure a saved assessment into an active dashboard run dictionary.

    Returns None if the run does not exist or fails validation.
    """
    rec = load_saved_assessment(run_id, saved_dir=saved_dir)
    if not rec or not isinstance(rec, dict):
        return None
    if not rec.get("run_id") or not rec.get("geom_wkt") or not rec.get("metrics"):
        return None

    meta = rec.get("meta") or {}
    claim_info = rec.get("claim_info") or meta.get("claim_info") or {}
    workflow_mode = (
        rec.get("workflow_mode")
        or claim_info.get("workflow_mode")
        or meta.get("workflow_mode")
        or ("recent" if meta.get("workflow") == "incident_comparison" and "trajectory_pre" in meta else "historical")
    )
    incident_date = (
        rec.get("incident_date")
        or claim_info.get("incident_date")
        or meta.get("incident_date")
    )
    claimed_peril = (
        rec.get("claimed_peril")
        or claim_info.get("claimed_cause")
        or meta.get("peril")
        or "General Crop Stress"
    )
    discovery = rec.get("discovery")
    if not discovery and meta.get("workflow") == "incident_comparison":
        discovery = {
            "selected_pre": meta.get("pre_scene"),
            "selected_post": meta.get("post_scene"),
            "trajectory_pre": meta.get("trajectory_pre", []),
            "trajectory_post": meta.get("trajectory_post", []),
            "data_sufficiency": "sufficient" if meta.get("data_state") == "valid" else "insufficient",
            "notes": meta.get("notes", ""),
        }

    return {
        "run_id": rec["run_id"],
        "param_key": rec.get("param_key"),
        "mode": rec.get("mode", "demo"),
        "workflow_mode": workflow_mode,
        "incident_date": incident_date,
        "claimed_peril": claimed_peril,
        "discovery": discovery,
        "event_window": rec.get("event_window"),
        "baseline_years": rec.get("baseline_years"),
        "geom_wkt": rec["geom_wkt"],
        "metrics": rec.get("metrics", {}),
        "res": rec,
        "grid_summary": rec.get("grid_summary", {}),
        "claim_info": claim_info,
        "timestamp": rec.get("timestamp"),
    }

