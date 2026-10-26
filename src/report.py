#!/usr/bin/env python3
"""FarmAid - Stage 4: downloadable evidence reports (ReportLab).

Two report builders, both generated from CURRENT analysis outputs (no
invented values; missing inputs produce a clear message, not silence):

  - build_report()         region/grid overview report
                           -> reports/FarmAid_damage_report.pdf
  - build_field_report()   per-field report (farmer workflow)
                           -> reports/FarmAid_field_report.pdf

Usage:
  python src/report.py            # region overview (needs analyze.py outputs)
  python src/field.py ...         # field workflow writes its own PDF report
"""
import csv
import json
import os
import sys
from collections import Counter

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer,
                                Table, TableStyle)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from fetch_gee import load_config  # noqa: E402

DISCLAIMER = ("This report is a research-prototype output: it contains "
              "screening indicators, not verified crop-loss estimates. It "
              "is not an insurance decision, a claim settlement, or a "
              "crop-loss certificate.")

STYLES = None


def _styles():
    global STYLES
    if STYLES is None:
        base = getSampleStyleSheet()
        base.add(ParagraphStyle("Small", parent=base["BodyText"], fontSize=8.5,
                                textColor=colors.HexColor("#555555")))
        STYLES = base
    return STYLES


def _acquisition_table(meta):
    """Period -> image count / dates / coverage table (live data)."""
    rows = [["Period", "Images", "First", "Last", "Valid px %"]]
    for p in meta.get("baseline_periods", []):
        rows.append([f"Baseline {p['year']}",
                     str(p.get("images", "?")),
                     str(p.get("first", "?"))[:10],
                     str(p.get("last", "?"))[:10],
                     f"{p.get('coverage_pct', '?')}%"])
    ev = meta.get("event", {})
    rows.append(["Event window",
                 str(ev.get("images", "?")),
                 str(ev.get("first", "?"))[:10],
                 str(ev.get("last", "?"))[:10],
                 f"{ev.get('coverage_pct', '?')}%"])
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A4494")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    return t


def _metrics_table(rows):
    t = Table(rows, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#15203F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#F1F6EE")]),
    ]))
    return t


def report_baseline_years(meta):
    """Actual baseline years used (from per-period records), falling back
    to the configured list."""
    years = [p["year"] for p in meta.get("baseline_periods", [])
             if p.get("year")]
    return years if years else meta.get("baseline_years", "?")


def _mode_line(meta):
    if meta.get("synthetic") or meta.get("mode") == "demo":
        return "Mode: <b>DEMO — synthetic example data, not actual satellite observations</b>"
    return "Mode: <b>LIVE — real Earth Engine satellite observations</b>"


def load_json(path, required=True):
    if not os.path.exists(path):
        if required:
            sys.exit(f"Missing required file: {path}")
        return None
    with open(path) as fh:
        return json.load(fh)


def summarize_csv(path):
    """Aggregate the analysis summary CSV into report-ready numbers."""
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    n = len(rows)
    get = lambda r, k: r.get(k, "")
    severity = Counter(get(r, "severity") for r in rows)
    cause = Counter(get(r, "cause") for r in rows)

    def mean_of(key, default=0.0):
        vals = []
        for r in rows:
            try:
                vals.append(float(get(r, key)))
            except (TypeError, ValueError):
                continue
        return round(sum(vals) / len(vals), 2) if vals else default

    return {
        "n_cells": n,
        "n_negative": sum(1 for r in rows if get(r, "severity") != "none"),
        "severity": dict(severity),
        "cause": dict(cause),
        "mean_valid_pct": mean_of("valid_pct"),
        "mean_affected_pct": mean_of("affected_pct"),
        "mean_ndvi_delta": mean_of("ndvi_delta"),
        "mean_z": mean_of("z_score"),
    }


# --------------------------------------------------------------------------- #
# Region / grid overview report
# --------------------------------------------------------------------------- #
def build_report(cfg, meta, summary, quicklook_path, out_path):
    styles = _styles()
    h1, h2, body, small, italic = (styles["Title"], styles["Heading2"],
                                   styles["BodyText"], styles["Small"],
                                   styles["Italic"])

    story = [
        Paragraph("FarmAid — Provisional Damage Screening Report", h1),
        Paragraph(f"Study area: {meta.get('aoi_center', ('?', '?'))[0]}, "
                  f"{meta.get('aoi_center', ('?', '?'))[1]} "
                  f"({meta.get('crs', '?')})", body),
        Paragraph("Research prototype — academic demonstration", small),
        Spacer(1, 6 * mm),
        Paragraph(f"⚠️ {DISCLAIMER}", italic),
        Spacer(1, 6 * mm),
        Paragraph("1. Assessment periods & data source", h2),
        Paragraph(_mode_line(meta), body),
        Paragraph(
            f"Collection: {meta.get('collection') or 'n/a (synthetic)'} · "
            f"resolution {meta.get('scale_m')} m · CRS {meta.get('crs')}",
            body),
        Paragraph(
            f"Observation window: {meta.get('event_window')} (year "
            f"{meta.get('event_year')}) · Baseline: "
            f"{meta.get('baseline_window')} across "
            f"{report_baseline_years(meta)}", body),
        Spacer(1, 3 * mm),
    ]
    if meta.get("skipped_baseline_years"):
        story.append(Paragraph(
            "Baseline years skipped (no usable imagery): "
            + ", ".join(f"{y['year']}" for y in
                        meta["skipped_baseline_years"][:4]), small))
    if not meta.get("synthetic"):
        story.append(_acquisition_table(meta))
        story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("2. Analytical method (provisional)", h2))
    story.append(Paragraph(
        "NDVI = (NIR−R)/(NIR+R); NDWI = (G−NIR)/(G+NIR) (water-related "
        "indicator only). Anomaly z = (observed NDVI − baseline mean) / "
        f"max(std, 0.02), threshold {cfg.get('ndvi_z_threshold')}. "
        f"Severity (affected valid-pixel fraction, lower bounds): "
        f"{cfg.get('severity')}. Cause: flood = anomaly + high NDWI; "
        "drought = strong anomaly with context; else unknown. Hail is "
        "unsupported in the MVP.", body))
    story.append(Paragraph(
        "Cells are analytical units (~300 m) aligned to the raster grid — "
        "pseudo-fields, not farm parcels.", small))

    story.append(Paragraph("3. Results (from current analysis outputs)", h2))
    sev = summary["severity"]
    cause = summary["cause"]
    res_rows = [["Metric", "Value"],
                ["Analytical cells", str(summary["n_cells"])],
                ["Cells w/ negative anomaly",
                 f"{summary['n_negative']} "
                 f"({100.0 * summary['n_negative'] / max(summary['n_cells'], 1):.1f}%)"],
                ["Severity — none / low / moderate / high",
                 f"{sev.get('none', 0)} / {sev.get('low', 0)} / "
                 f"{sev.get('moderate', 0)} / {sev.get('high', 0)}"],
                ["Cause — flood / drought / unknown",
                 f"{cause.get('flood', 0)} / {cause.get('drought', 0)} / "
                 f"{cause.get('unknown', 0)}"],
                ["Mean valid-data coverage", f"{summary['mean_valid_pct']}%"],
                ["Mean affected fraction (valid px)",
                 f"{summary['mean_affected_pct']}%"],
                ["Mean ΔNDVI (observed − baseline)",
                 str(summary["mean_ndvi_delta"])],
                ["Mean anomaly z-score", str(summary["mean_z"])],
                ]
    story.append(_metrics_table(res_rows))

    if quicklook_path and os.path.exists(quicklook_path):
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Quicklook: observation NDVI vs negative "
                               "anomaly mask", body))
        story.append(Image(quicklook_path, width=150 * mm, height=60 * mm))

    story.append(Paragraph("4. Limitations", h2))
    story.append(Paragraph(
        "- Single observation window; no multi-date trajectory (hail "
        "classification unsupported).\n"
        "- Severity thresholds are provisional — no ground-truth validation "
        "in the MVP.\n"
        "- Grid cells are analytical units, not cadastral parcels; NDVI "
        "decline ≠ verified crop loss.\n"
        "- NDWI is a water-related indicator, not proof of flooding.\n"
        f"- {DISCLAIMER}", body))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        "Generated by FarmAid (research prototype) — "
        f"source.json mode: {meta.get('mode', 'unknown')}", small))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            rightMargin=18 * mm, leftMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    doc.build(story)
    return out_path


# --------------------------------------------------------------------------- #
# Per-field report (selected boundary)
# --------------------------------------------------------------------------- #
HA_TO_ACRES = 2.47105


def generate_field_insights(metrics, meta):
    """Generate plain-language insights from the actual run metrics.

    Every insight is traceable to the current run's calculations.  This
    function never produces unsupported claims or generic advice.
    """
    m = metrics
    insights = []

    area_ha = m.get("area_ha", 0)
    area_ac = round(area_ha * HA_TO_ACRES, 3)
    insights.append(
        f"The assessed field covers {area_ha} ha ({area_ac} acres).")

    # --- Evidence state ---
    if m.get("insufficient_data"):
        insights.append(
            f"Insufficient valid satellite data (coverage {m.get('valid_pct', 0)}%) "
            "— no reliable assessment can be formed. Low valid coverage is a "
            "data limitation (cloud masking, terrain shadows, or sensor gaps), "
            "NOT evidence of healthy crops.")
        return insights

    if m.get("no_evidence_of_damage"):
        insights.append(
            f"No evidence of significant damage detected under the current "
            f"thresholds and available data (sufficient coverage {m.get('valid_pct', 0)}%).")

    # --- NDVI change interpretation ---
    delta = m.get("ndvi_delta")
    ndvi_mean = m.get("ndvi_mean")
    z = m.get("z_score")
    if delta is not None:
        if delta > 0:
            insights.append(
                f"Observed NDVI ({ndvi_mean}) is HIGHER than the historical "
                f"baseline by {abs(delta):.3f} (positive ΔNDVI = +{delta}). "
                "This indicates the vegetation during the observation window "
                "was greener than usual for the same calendar period, which "
                "does NOT indicate crop damage. Note: a positive anomaly "
                "does not prove crop health — seasonal variation, irrigation "
                "or crop rotation changes can also raise NDVI.")
        elif delta < -0.05:
            insights.append(
                f"Observed NDVI ({ndvi_mean}) is LOWER than the historical "
                f"baseline by {abs(delta):.3f} (ΔNDVI = {delta}). "
                "A decline in NDVI between the baseline and observation "
                "window is consistent with vegetation stress, but could "
                "also result from seasonal variation, harvesting, cloud "
                "contamination or land-use change.")
        else:
            insights.append(
                f"Observed NDVI ({ndvi_mean}) is close to the baseline "
                f"(ΔNDVI = {delta}). Minor deviations within ±0.05 are "
                "typically within normal variability.")

    # --- Z-score interpretation ---
    if z is not None:
        if z > 0:
            insights.append(
                f"The mean anomaly z-score is +{z:.2f} (positive), meaning "
                "observed NDVI is above the baseline mean relative to its "
                "variability. A positive z-score indicates conditions above "
                "the historical norm — it does NOT indicate crop damage.")
        elif z < -1.5:
            insights.append(
                f"The mean anomaly z-score is {z:.2f} (strongly negative), "
                "indicating observed vegetation is well below the historical "
                "norm relative to its variability. This is consistent with "
                "stress but is a screening indicator, not proof of crop loss.")
        else:
            insights.append(
                f"The mean anomaly z-score is {z:.2f}, indicating a moderate "
                "deviation from the historical baseline.")

    # --- Affected fraction ---
    aff = m.get("affected_pct", 0)
    aff_ha = m.get("affected_area_ha", 0)
    aff_ac = round(aff_ha * HA_TO_ACRES, 3) if aff_ha else 0
    severity = m.get("severity", "unknown")
    cause = m.get("cause", "unknown")
    if aff < 1.0:
        insights.append(
            f"Only {aff}% of valid pixels show a negative anomaly "
            f"(est. {aff_ha} ha / {aff_ac} ac). With such a small fraction, "
            f"the provisional severity is '{severity}'. A small or zero "
            "affected fraction means the screening did NOT detect widespread "
            "vegetation decline — this is not proof that the crop is healthy, "
            "only that the anomaly threshold was not exceeded at the "
            "pixel level.")
    elif aff < 10:
        insights.append(
            f"{aff}% of valid pixels show a negative anomaly "
            f"(est. {aff_ha} ha / {aff_ac} ac of the {area_ha} ha field). "
            f"Provisional severity: '{severity}'. This is a modest anomaly "
            "that could represent localized stress or noise.")
    else:
        insights.append(
            f"{aff}% of valid pixels show a negative anomaly "
            f"(est. {aff_ha} ha / {aff_ac} ac). "
            f"Provisional severity: '{severity}', cause: '{cause}'. "
            "This fraction represents the screening estimate of affected "
            "area within the selected field.")

    # --- Severity/cause rationale ---
    if severity == "high":
        insights.append(
            "High severity indicates widespread, substantial negative vegetation "
            "anomalies across more than 35% of valid field pixels.")
    elif severity == "moderate":
        insights.append(
            "Moderate severity indicates between 15% and 35% of valid field pixels "
            "exhibit negative vegetation anomalies.")
    elif severity == "none" and aff > 0:
        insights.append(
            f"Severity is 'none' even though {aff}% of pixels are flagged: "
            "the affected fraction is below the configured threshold for "
            "'low' severity (≥5%). A few anomalous pixels may be noise, "
            "mixed pixels, or field-edge effects.")

    if cause == "flood":
        insights.append(
            "Waterlogging / inundation detected: elevated NDWI accompanies the "
            "vegetation anomaly, suggesting standing water or saturated soil. "
            "This is a satellite screening indicator, not ground-verified flood damage.")
    elif cause == "drought":
        insights.append(
            "Persistent moisture deficit / drought pattern detected: prolonged "
            "negative NDVI anomaly without elevated NDWI surface water signal. "
            "Ground verification is recommended to confirm soil moisture conditions.")
    elif cause == "unknown" and aff > 5:
        insights.append(
            "Possible cause: Cannot determine from satellite data alone. "
            "Satellite indicators do not show distinct flood (elevated NDWI) or "
            "prolonged drought patterns. This does not rule out waterlogging, moisture "
            "deficit, disease, pests, or field management factors; ground inspection is needed.")


    # --- Data quality ---
    valid_pct = m.get("valid_pct", 0)
    n_valid = m.get("n_valid_pixels")
    if valid_pct < 50:
        insights.append(
            f"Only {valid_pct}% of pixels have valid data. Low coverage "
            "substantially reduces confidence — cloud contamination, "
            "terrain shadows, or partial overlap with the raster are "
            "likely causes. Results should be treated with high uncertainty.")
    elif valid_pct < 80:
        insights.append(
            f"Valid data coverage is {valid_pct}%"
            + (f" ({n_valid} pixels)" if n_valid else "")
            + ". Some pixels are masked, moderately reducing confidence.")
    else:
        insights.append(
            f"Valid data coverage is high ({valid_pct}%"
            + (f", {n_valid} pixels" if n_valid else "")
            + "), supporting reasonable confidence in the screening results.")

    # --- Image count / baseline years ---
    ev = meta.get("event", {})
    n_images = ev.get("images")
    if n_images is not None:
        if n_images < 3:
            insights.append(
                f"Only {n_images} cloud-filtered event image(s) were used. "
                "Fewer images increase the chance that residual cloud "
                "contamination or a single atypical observation drives the "
                "composite. More images generally improve reliability.")
        else:
            insights.append(
                f"{n_images} event images were used in the composite, "
                "providing reasonable temporal averaging.")
    skipped = meta.get("skipped_baseline_years", [])
    periods = meta.get("baseline_periods", [])
    if skipped:
        insights.append(
            f"{len(skipped)} baseline year(s) were skipped due to "
            f"insufficient imagery: "
            + ", ".join(str(y.get("year", "?")) for y in skipped[:4])
            + f". {len(periods)} baseline year(s) were used. "
            "Fewer baseline years reduce the baseline's representativeness "
            "and increase uncertainty in the anomaly z-score.")

    # --- Uncertainty and further evidence ---
    unc = m.get("uncertainty", "unknown")
    if unc == "high":
        insights.append(
            "Overall uncertainty is HIGH. The available evidence is "
            "insufficient for a confident assessment. Consider: "
            "(1) checking a wider observation window, "
            "(2) comparing with an adjacent unaffected field, "
            "(3) field-level ground verification, or "
            "(4) multi-temporal analysis to distinguish genuine stress "
            "from seasonal or atmospheric effects.")
    elif unc == "medium":
        insights.append(
            "Uncertainty is MEDIUM. The indicators suggest a possible "
            "cause but lack the strength for high confidence. "
            "Ground verification or multi-temporal comparison would "
            "strengthen the assessment.")

    insights.append(
        "IMPORTANT: This is a screening tool. Zero anomalies or missing "
        "pixels do not prove the absence of crop damage. Satellite "
        "observations cannot detect all damage types (e.g. hail, "
        "root diseases, herbicide drift) and are limited by cloud cover, "
        "image timing, and pixel resolution.")

    return insights


def build_field_report(cfg, metrics, meta, quicklook_path, out_path):
    """Report for ONE selected field, from the field assessment outputs."""
    styles = _styles()
    h1, h2, body, small, italic = (styles["Title"], styles["Heading2"],
                                   styles["BodyText"], styles["Small"],
                                   styles["Italic"])

    area_ha = metrics.get('area_ha', '?')
    area_ac = round(area_ha * HA_TO_ACRES, 2) if isinstance(area_ha,
                                                              (int, float)
                                                              ) else '?'

    story = [
        Paragraph("FarmAid — Field Assessment Report", h1),
        Paragraph(f"Run: <b>{metrics.get('run_id', 'n/a')}</b> · research "
                  "prototype", small),
        Spacer(1, 5 * mm),
        Paragraph(f"⚠️ {DISCLAIMER}", italic),
        Spacer(1, 4 * mm),
        Paragraph("1. Field identity", h2),
        Paragraph(
            f"Area: <b>{area_ha} ha</b> (~{area_ac} acres, "
            "UTM-projected plane area of the selected boundary). "
            "The boundary is a user-selected ASSESSMENT area — it is NOT "
            "proof of legal ownership, and no land-record integration is "
            "claimed.", body),
    ]
    if metrics.get("bounds_wgs84"):
        story.append(Paragraph(
            f"Bounds (WGS84): {metrics['bounds_wgs84']} · centroid "
            f"{metrics.get('centroid_wgs84')}", small))
    story.append(Paragraph(
        f"Assessment periods: observation "
        f"{meta.get('event_window')} · baseline "
        f"{meta.get('baseline_window')} across "
        f"{report_baseline_years(meta)}", body))
    if not meta.get("synthetic"):
        ev = meta.get("event", {})
        story.append(Paragraph(
            f"Actual acquisitions: {ev.get('images', '?')} event images "
            f"({str(ev.get('first', '?'))[:10]} → "
            f"{str(ev.get('last', '?'))[:10]})",
            small))
    if meta.get("skipped_baseline_years"):
        story.append(Paragraph(
            "Baseline years skipped (no usable imagery): "
            + ", ".join(f"{y['year']} ({str(y['reason'])[:70]})"
                        for y in meta["skipped_baseline_years"][:4]),
            small))
    if meta.get("event", {}).get("fallback_used"):
        story.append(Paragraph(
            "Observation window widened by fallback: "
            f"{meta['event'].get('start')} → {meta['event'].get('end')}",
            small))
    story.append(Spacer(1, 3 * mm))
    story.append(Paragraph("2. Data source & acquisition", h2))
    story.append(Paragraph(_mode_line(meta), body))
    story.append(Paragraph(f"CRS {meta.get('crs', '?')} · "
                           f"{meta.get('scale_m', '?')} m pixels", body))
    story.append(Spacer(1, 2 * mm))
    if not meta.get("synthetic"):
        story.append(_acquisition_table(meta))
        story.append(Spacer(1, 3 * mm))

    story.append(Paragraph("3. Field indicators (provisional)", h2))
    m = metrics
    if m.get("insufficient_data"):
        story.append(Paragraph(
            "⚠️ <b>Insufficient valid data</b> — "
            f"coverage {m.get('valid_pct', '?')}% is below the "
            f"configured minimum. No severity/cause conclusion is offered; "
            "clouds, empty imagery or shadows are the likely cause. "
            "Insufficient data is NOT the same as 'no damage'.", body))
    if m.get("no_evidence_of_damage"):
        story.append(Paragraph(
            "✓ Sufficient valid data and NO negative anomaly detected — "
            "reported as no evidence of damage (not as a guarantee).",
            body))

    aff_ha = m.get("affected_area_ha", "?")
    aff_ac = round(aff_ha * HA_TO_ACRES, 3) if isinstance(aff_ha,
                                                            (int, float)
                                                            ) else "?"
    rows = [["Indicator", "Value"],
            ["Field area", f"{area_ha} ha (~{area_ac} ac)"],
            ["Usable satellite data", f"{m.get('valid_pct', '?')}%"],
            ["Area showing possible crop stress",
             f"{m.get('affected_pct', '?')}% of usable pixels"],
            ["Estimated area with possible stress",
             f"{aff_ha} ha (~{aff_ac} ac) — screening estimate, "
             "not confirmed crop loss"],
            ["Screening level", f"{str(m.get('severity', '?')).title()} (provisional classification)"],
            ["Possible cause", f"{str(m.get('cause', '?')).title()} (satellite indicator pattern)"],
            ["Confidence / Uncertainty", f"{str(m.get('uncertainty', '?')).title()} uncertainty"],
            ["Mean NDVI (observed)", m.get("ndvi_mean", "?")],
            ["ΔNDVI (observed − baseline)", m.get("ndvi_delta", "?")],
            ["Anomaly z-score (mean)", m.get("z_score", "?")],
            ["Mean NDWI (water indicator)", m.get("ndwi_mean", "?")],
            ]
    story.append(_metrics_table(rows))

    if quicklook_path and os.path.exists(quicklook_path):
        story.append(Spacer(1, 4 * mm))
        story.append(Paragraph("Before/after screening: baseline NDVI vs "
                               "observed NDVI vs anomaly mask", body))
        story.append(Image(quicklook_path, width=150 * mm, height=48 * mm))

    # --- Section 4: Assessment Insights (new) ---
    story.append(Paragraph("4. Assessment insights", h2))
    story.append(Paragraph(
        "The following plain-language explanations are generated from "
        "this run's actual metrics. Every statement is traceable to the "
        "calculations above; no claims are made beyond the data.",
        small))
    story.append(Spacer(1, 2 * mm))
    for insight in generate_field_insights(metrics, meta):
        story.append(Paragraph(f"• {insight}", body))
        story.append(Spacer(1, 1.5 * mm))

    story.append(Paragraph("5. Method & limitations", h2))
    story.append(Paragraph(
        "Method: Sentinel-2 surface reflectance, SCL+QA60 cloud/shadow "
        "masking, same-calendar-window multi-year baseline (median + std), "
        f"z = (obs − mean)/max(std, 0.02) with threshold "
        f"{cfg.get('ndvi_z_threshold')}; severity lower bounds "
        f"{cfg.get('severity')}; cause rules flood/drought/unknown with "
        "uncertainty labels.", body))
    story.append(Paragraph(
        "- Hail is NOT classified in this prototype (requires multi-date "
        "trajectory evidence); suspect cells return cause 'unknown'.\n"
        "- The drawn boundary is not an ownership record; affected-area is "
        "a screening estimate, not measured crop loss.\n"
        "- Severity thresholds are provisional, pending ground-truth "
        "validation; NDWI is a water-related indicator only.\n"
        "- Snow, terrain shadows, water bodies and non-agricultural "
        "surfaces are data limitations: anomalies there are NOT interpreted "
        "as crop damage, and inadequate coverage is reported as "
        "'insufficient data' rather than as damage.\n"
        f"- {DISCLAIMER}", body))
    story.append(Spacer(1, 5 * mm))
    story.append(Paragraph(
        "Generated by FarmAid (research prototype) — mode: "
        f"{meta.get('mode', 'unknown')}", small))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            rightMargin=18 * mm, leftMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    doc.build(story)
    return out_path


def main():
    cfg = load_config(os.path.join(ROOT, "config.yaml"))
    meta = load_json(os.path.join(ROOT, cfg["data_dir"], "processed",
                                  "source.json"))
    summary_path = os.path.join(ROOT, cfg["out_dir"], "damage_summary.csv")
    if not os.path.exists(summary_path):
        sys.exit(f"Missing {summary_path}. Run `python src/analyze.py` "
                 f"first (after `python src/fetch_gee.py` or --demo).")
    summary = summarize_csv(summary_path)
    quicklook = os.path.join(ROOT, cfg["out_dir"], "quicklook.png")
    out_path = os.path.join(ROOT, cfg.get("report_dir", "reports"),
                            "FarmAid_damage_report.pdf")
    build_report(cfg, meta, summary, quicklook, out_path)
    print("Report written ->", out_path)


if __name__ == "__main__":
    main()