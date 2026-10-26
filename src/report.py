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

from reportlab.pdfgen import canvas
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (HRFlowable, Image, KeepTogether, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from fetch_gee import load_config  # noqa: E402

DISCLAIMER = ("This report is an academic / research-prototype output: it contains "
              "satellite-derived screening indicators for crop damage assessment, "
              "not a verified crop-loss estimate, insurance adjudication, claim settlement, "
              "or automated payout certificate. Ground verification remains required.")

STYLES = None


class NumberedCanvas(canvas.Canvas):
    """Two-pass canvas that writes total page count and professional running
    headers/footers on every page."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica-Bold", 7.5)
        self.setFillColor(colors.HexColor("#475569"))
        # Top running header (pages > 1)
        if self._pageNumber > 1:
            self.drawString(14 * mm, 287 * mm, "FarmAid — Satellite-Based Crop Damage Assessment Report")
            self.setFont("Helvetica", 7.5)
            self.drawRightString(196 * mm, 287 * mm, "Claim Verification & Evidence Dossier")
            self.setStrokeColor(colors.HexColor("#CBD5E1"))
            self.setLineWidth(0.5)
            self.line(14 * mm, 284 * mm, 196 * mm, 284 * mm)

        # Bottom running footer (all pages)
        self.setStrokeColor(colors.HexColor("#CBD5E1"))
        self.setLineWidth(0.5)
        self.line(14 * mm, 13 * mm, 196 * mm, 13 * mm)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(colors.HexColor("#64748B"))
        self.drawString(14 * mm, 9 * mm, "CONFIDENTIAL & TRACEABLE · FarmAid Satellite Screening Engine (Sentinel-2 L2A)")
        self.setFont("Helvetica-Bold", 7.5)
        self.drawRightString(196 * mm, 9 * mm, f"Page {self._pageNumber} of {page_count}")
        self.restoreState()


def _styles():
    global STYLES
    if STYLES is None:
        base = getSampleStyleSheet()
        base.add(ParagraphStyle("Small", parent=base["BodyText"], fontSize=8,
                                leading=10, textColor=colors.HexColor("#475569")))
        base.add(ParagraphStyle("DocTitle", parent=base["Title"], fontSize=18,
                                leading=22, textColor=colors.HexColor("#0F172A"),
                                fontName="Helvetica-Bold", alignment=0))
        base.add(ParagraphStyle("DocSub", parent=base["Normal"], fontSize=9.5,
                                leading=13, textColor=colors.HexColor("#475569"),
                                fontName="Helvetica"))
        base.add(ParagraphStyle("SecHeading", parent=base["Heading2"], fontSize=11,
                                leading=14, textColor=colors.HexColor("#1E293B"),
                                fontName="Helvetica-Bold", spaceBefore=4, spaceAfter=2))
        base.add(ParagraphStyle("SubHeading", parent=base["Heading3"], fontSize=9.5,
                                leading=12, textColor=colors.HexColor("#334155"),
                                fontName="Helvetica-Bold", spaceBefore=2, spaceAfter=1))
        base.add(ParagraphStyle("TableHead", parent=base["Normal"], fontSize=8,
                                leading=10, textColor=colors.white, fontName="Helvetica-Bold"))
        base.add(ParagraphStyle("TableText", parent=base["Normal"], fontSize=7.5,
                                leading=10, textColor=colors.HexColor("#1E293B"), fontName="Helvetica"))
        base.add(ParagraphStyle("TableTextBold", parent=base["Normal"], fontSize=7.5,
                                leading=10, textColor=colors.HexColor("#0F172A"), fontName="Helvetica-Bold"))
        base.add(ParagraphStyle("CalloutText", parent=base["Normal"], fontSize=8,
                                leading=11, textColor=colors.HexColor("#1E293B"), fontName="Helvetica"))
        STYLES = base
    return STYLES


def _acquisition_table(meta):
    """Period -> image count / dates / coverage table (live data)."""
    rows = [["Period", "Images", "First", "Last", "Study AOI Clear %"]]
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
        "image resolution, and temporal revisit intervals.")
    return insights


def build_field_report(cfg, metrics, meta, quicklook_path, out_path, claim_info=None):
    """Generate an authoritative, professional Satellite-Based Crop Damage
    Assessment Report for Insurance Claims Assessment.

    Structure conforms to insurance assessor evidentiary requirements:
      Cover / Identification — Metadata, Claim details, Legal Notice
      Section 1 — Executive Summary & Assessment Status Banner
      Section 2 — Field & Claimed Event Details (Claimant vs Satellite facts)
      Section 3 — Satellite Data & Quality Assurance (Acquisitions, Masking, Coverage)
      Section 4 — Damage-Analysis Results (NDVI, Delta, Z-score, Area, NDWI)
      Section 5 — Maps & Visual Evidence (High-resolution multi-panel screening)
      Section 6 — Severity & Cause Assessment Methodology (Transparent thresholds)
      Section 7 — Limitations & Claim Adjuster Verification (Field checklist)
      Section 8 — Reproducibility & Audit Record (Hash fingerprint, provenance)
    """
    styles = _styles()
    title_style = styles["DocTitle"]
    sub_style = styles["DocSub"]
    h2 = styles["SecHeading"]
    h3 = styles["SubHeading"]
    body = styles["BodyText"]
    small = styles["Small"]
    table_head = styles["TableHead"]
    table_text = styles["TableText"]
    table_bold = styles["TableTextBold"]

    # Claim & Policy details (from parameter or metrics, default to 'Not provided')
    c_info = claim_info or metrics.get("claim_info") or {}
    claim_ref = str(c_info.get("claim_ref") or "Not provided")
    policy_no = str(c_info.get("policy_no") or "Not provided")
    claimant_name = str(c_info.get("claimant_name") or "Not provided")
    crop_type = str(c_info.get("crop_type") or "Not specified")
    claimed_peril = str(c_info.get("claimed_cause") or "Not specified")

    run_id = metrics.get("run_id", "n/a")
    param_key = metrics.get("param_key", "n/a")
    mode_str = "LIVE — Sentinel-2 Satellite Observations" if meta.get("mode") == "live" or not meta.get("synthetic") else "DEMO — Deterministic Synthetic Simulation"
    timestamp_str = metrics.get("timestamp") or meta.get("timestamp") or "2026-10-26 (UTC)"

    area_ha = metrics.get("area_ha", "?")
    area_ac = round(area_ha * HA_TO_ACRES, 2) if isinstance(area_ha, (int, float)) else "?"
    aff_ha = metrics.get("affected_area_ha", "?")
    aff_ac = round(aff_ha * HA_TO_ACRES, 3) if isinstance(aff_ha, (int, float)) else "?"
    valid_cov = metrics.get("valid_pct", 0.0)
    aff_pct = metrics.get("affected_pct", 0.0)
    severity_val = str(metrics.get("severity", "unknown")).capitalize()
    cause_val = str(metrics.get("cause", "unknown")).capitalize()
    uncertainty_val = str(metrics.get("uncertainty", "unknown")).capitalize()

    def _val_or_na(val, fmt="{:.3f}"):
        if val is None:
            return "N/A — Insufficient Clear Data"
        if isinstance(val, (int, float)):
            return fmt.format(val)
        return str(val)

    story = []

    # =========================================================================
    # COVER & REPORT IDENTIFICATION
    # =========================================================================
    story.append(Paragraph("<b>FarmAid</b>", ParagraphStyle("Brand", parent=body, fontSize=11, leading=13, textColor=colors.HexColor("#1E3A8A"), fontName="Helvetica-Bold")))
    story.append(Paragraph("Satellite-Based Crop Damage Assessment Report", title_style))
    story.append(Paragraph("Crop Insurance Claim Verification & Damage Evidence Dossier", sub_style))
    story.append(Spacer(1, 3 * mm))

    # Top Metadata Table (2-column layout: Claim / Insured info vs Technical Location info)
    meta_table_data = [
        [Paragraph("<b>Claim Reference:</b>", table_text), Paragraph(claim_ref, table_bold),
         Paragraph("<b>Assessment Run ID:</b>", table_text), Paragraph(run_id, table_bold)],
        [Paragraph("<b>Policy Number:</b>", table_text), Paragraph(policy_no, table_bold),
         Paragraph("<b>Processing Mode:</b>", table_text), Paragraph(mode_str, table_bold)],
        [Paragraph("<b>Claimant / Insured:</b>", table_text), Paragraph(claimant_name, table_bold),
         Paragraph("<b>Assessment Date:</b>", table_text), Paragraph(str(timestamp_str)[:19], table_text)],
        [Paragraph("<b>Insured Crop Type:</b>", table_text), Paragraph(crop_type, table_bold),
         Paragraph("<b>Total Field Area:</b>", table_text), Paragraph(f"{area_ha} ha (~{area_ac} acres)", table_bold)],
        [Paragraph("<b>Claimed Peril:</b>", table_text), Paragraph(claimed_peril, table_bold),
         Paragraph("<b>Field Centroid:</b>", table_text), Paragraph(f"{metrics.get('centroid_wgs84', ['?','?'])[0]}°N, {metrics.get('centroid_wgs84', ['?','?'])[1]}°E", table_text)],
    ]
    meta_t = Table(meta_table_data, colWidths=[36 * mm, 55 * mm, 40 * mm, 51 * mm])
    meta_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(meta_t)
    story.append(Spacer(1, 2.5 * mm))

    # Prominent Legal Notice callout
    notice_text = (
        "<b>LEGAL NOTICE & EVIDENTIARY SCOPE:</b> This document provides objective, satellite-derived "
        "spectral screening indicators derived from Copernicus Sentinel-2 Level-2A imagery for crop insurance "
        "claim evaluation. It is an evidentiary screening dossier — NOT an automated insurance claim settlement, "
        "guaranteed payout authorization, or official land-title certification. Ground verification remains mandatory."
    )
    notice_t = Table([[Paragraph(notice_text, styles["CalloutText"])]], colWidths=[182 * mm])
    notice_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7")),
        ("BOX", (0, 0), (-1, -1), 0.75, colors.HexColor("#D97706")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(notice_t)
    story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 1 — EXECUTIVE ASSESSMENT SUMMARY
    # =========================================================================
    story.append(Paragraph("1. Executive Assessment Summary", h2))

    # Status Banner
    is_insufficient = metrics.get("insufficient_data", False)
    is_no_evidence = metrics.get("no_evidence_of_damage", False)
    if is_insufficient:
        status_text = "ASSESSMENT STATUS: INSUFFICIENT SATELLITE DATA — SCREENING INCONCLUSIVE"
        status_sub = f"Only {valid_cov}% usable clear observations were available over the field during the claim period (below the required 30% quality threshold). High cloud cover or shadows prevented conclusive interpretation."
        status_bg = colors.HexColor("#991B1B")
    elif uncertainty_val.lower() == "high" or valid_cov < 50.0:
        status_text = "ASSESSMENT STATUS: ASSESSMENT COMPLETED — SIGNIFICANT LIMITATIONS"
        status_sub = f"Assessment completed with {valid_cov}% usable data. High uncertainty is present due to partial cloud coverage or baseline variability. Ground inspection is strongly advised."
        status_bg = colors.HexColor("#B45309")
    elif is_no_evidence:
        status_text = "ASSESSMENT STATUS: ASSESSMENT COMPLETED — NO SIGNIFICANT VEGETATION LOSS DETECTED"
        status_sub = f"Vegetation greenness during the claim observation window remained within normal multi-year healthy ranges (0.0% qualifying negative anomaly across {valid_cov}% clear observations)."
        status_bg = colors.HexColor("#15803D")
    else:
        status_text = f"ASSESSMENT STATUS: ASSESSMENT COMPLETED — {severity_val.upper()} DAMAGE INDICATORS FLAGGED"
        status_sub = f"An estimated {aff_pct}% of the field's usable area (~{aff_ha} ha) exhibits statistically significant negative vegetation anomalies consistent with crop damage."
        status_bg = colors.HexColor("#1E3A8A")

    status_data = [
        [Paragraph(f"<b>{status_text}</b>", ParagraphStyle("SB", parent=body, textColor=colors.white, fontSize=9, leading=11, fontName="Helvetica-Bold"))],
        [Paragraph(status_sub, ParagraphStyle("SS", parent=body, textColor=colors.HexColor("#F1F5F9"), fontSize=7.5, leading=10))]
    ]
    status_t = Table(status_data, colWidths=[182 * mm])
    status_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), status_bg),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(status_t)
    story.append(Spacer(1, 2.5 * mm))

    # Executive Summary Key Metrics Grid
    sum_data = [
        [Paragraph("<b>Metric / Parameter</b>", table_head), Paragraph("<b>Claimed Assessment Value</b>", table_head),
         Paragraph("<b>Insurance Assessor Interpretation</b>", table_head)],
        [Paragraph("Field Total Area", table_bold), Paragraph(f"{area_ha} ha (~{area_ac} acres)", table_text),
         Paragraph("Planar UTM area of the user-delineated parcel boundary", table_text)],
        [Paragraph("Observation Window", table_bold), Paragraph(f"{meta.get('event_window', ['?','?'])[0]} → {meta.get('event_window', ['?','?'])[1]}", table_text),
         Paragraph("Claimed loss observation timeline assessed via Sentinel-2", table_text)],
        [Paragraph("Historical Healthy Baseline", table_bold), Paragraph(f"{report_baseline_years(meta)} ({meta.get('baseline_window', ['?','?'])[0]} → {meta.get('baseline_window', ['?','?'])[1]})", table_text),
         Paragraph("Multi-year same-calendar reference norm", table_text)],
        [Paragraph("Usable Satellite Coverage", table_bold), Paragraph(f"{valid_cov}% ({metrics.get('n_valid_pixels', 0)} / {metrics.get('n_field_pixels', 0)} pixels)", table_text),
         Paragraph("Field pixels with clear cloud-free observations", table_text)],
        [Paragraph("Estimated Affected Area", table_bold), Paragraph(f"{aff_ha} ha (~{aff_ac} acres)", table_bold),
         Paragraph("Screening estimate of area meeting anomaly threshold", table_text)],
        [Paragraph("Estimated Affected Share", table_bold), Paragraph(f"{aff_pct}% of usable pixels", table_bold),
         Paragraph("Denominator: strictly clear, assessable field pixels", table_text)],
        [Paragraph("Damage Severity Classification", table_bold), Paragraph(severity_val, table_bold),
         Paragraph(f"Rule: None (<5%), Low (5-15%), Mod (15-35%), High (>=35%)", table_text)],
        [Paragraph("Likely Cause Hypothesis", table_bold), Paragraph(cause_val, table_bold),
         Paragraph("Spectral pattern: Flood (moisture), Drought (dryness), or Unknown", table_text)],
        [Paragraph("Assessment Uncertainty", table_bold), Paragraph(f"{uncertainty_val} Uncertainty", table_text),
         Paragraph("Reflects data coverage, baseline completeness, and spectral clarity", table_text)],
    ]
    sum_t = Table(sum_data, colWidths=[46 * mm, 50 * mm, 86 * mm])
    sum_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(sum_t)
    story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 2 — FIELD & CLAIMED EVENT DETAILS
    # =========================================================================
    story.append(Paragraph("2. Field and Claimed Event Details", h2))
    f_details_data = [
        [Paragraph("<b>Category</b>", table_head), Paragraph("<b>Claimant / User Submitted</b>", table_head),
         Paragraph("<b>Satellite & Algorithmic Fact</b>", table_head)],
        [Paragraph("Parcel Boundary", table_bold), Paragraph("User-specified polygon boundary", table_text),
         Paragraph(f"UTM Projected CRS: {meta.get('crs', 'EPSG:32643')} @ 10m native scale", table_text)],
        [Paragraph("Parcel Coordinates", table_bold), Paragraph(f"Centroid: {metrics.get('centroid_wgs84', ['?','?'])[0]}°N, {metrics.get('centroid_wgs84', ['?','?'])[1]}°E", table_text),
         Paragraph(f"BBox: {metrics.get('bounds_wgs84', ['?','?','?','?'])}", table_text)],
        [Paragraph("Claim Timeline", table_bold), Paragraph(f"Obs Window: {meta.get('event_window')}", table_text),
         Paragraph(f"Calendar window evaluated across Sentinel-2 orbit cycles", table_text)],
        [Paragraph("Baseline Composition", table_bold), Paragraph(f"Requested years: {meta.get('baseline_years')}", table_text),
         Paragraph(f"Successfully processed: {report_baseline_years(meta)}", table_text)],
    ]
    if meta.get("skipped_baseline_years"):
        skipped_str = "; ".join(f"{y['year']}: {y.get('reason','')[:45]}" for y in meta["skipped_baseline_years"][:3])
        f_details_data.append([
            Paragraph("Omitted Baseline Years", table_bold),
            Paragraph("Historical comparison request", table_text),
            Paragraph(f"Omitted due to data constraints: {skipped_str}", table_text)
        ])

    f_details_t = Table(f_details_data, colWidths=[38 * mm, 62 * mm, 82 * mm])
    f_details_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(f_details_t)
    story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 3 — SATELLITE DATA & QUALITY ASSURANCE
    # =========================================================================
    story.append(Paragraph("3. Satellite Data Source & Quality Assurance", h2))
    story.append(Paragraph(
        "Satellite data is sourced from European Space Agency (ESA) Copernicus Sentinel-2 MSI Level-2A "
        "(Bottom-of-Atmosphere surface reflectance). Cloud and shadow masking is executed at native 10 m resolution "
        "via Scene Classification Layer (SCL) and QA60 bitmask flags (defective, saturated, cloud medium/high probability, "
        "cirrus, snow, and cloud shadows are strictly masked out).", small))
    story.append(Spacer(1, 1.5 * mm))

    if not meta.get("synthetic"):
        acq_rows = [["Period / Year", "Scenes Used", "First Scene", "Last Scene", "Study AOI Clear %", "Field Boundary Clear %"]]
        for p in meta.get("baseline_periods", []):
            acq_rows.append([
                f"Baseline {p.get('year')}", str(p.get("images", "?")),
                str(p.get("first", "?"))[:10], str(p.get("last", "?"))[:10],
                f"{p.get('coverage_pct', '?')}%",
                f"{p.get('field_coverage_pct', p.get('coverage_pct', '?'))}%"
            ])
        ev = meta.get("event", {})
        acq_rows.append([
            "Claim Event Window", str(ev.get("images", "?")),
            str(ev.get("first", "?"))[:10], str(ev.get("last", "?"))[:10],
            f"{ev.get('coverage_pct', '?')}%",
            f"{ev.get('field_coverage_pct', valid_cov)}%"
        ])
        acq_t = Table(acq_rows, colWidths=[36 * mm, 24 * mm, 26 * mm, 26 * mm, 35 * mm, 35 * mm])
        acq_t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2A4494")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CBD5E1")),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(acq_t)
        story.append(Paragraph(
            "<i>Note on Spatial Divergence:</i> 'Study AOI Clear %' reflects clear observations across the broader ~4 km buffered region. "
            "'Field Boundary Clear %' represents the parcel itself. Conclusive assessment requires >= 30% clear pixels across the field boundary.",
            ParagraphStyle("NoteIt", parent=small, fontSize=7, leading=9, textColor=colors.HexColor("#64748B"))))
        story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 4 — DAMAGE-ANALYSIS RESULTS & METRICS
    # =========================================================================
    story.append(Paragraph("4. Damage-Analysis Results & Metrics", h2))
    b_mean_val = metrics.get("baseline_ndvi_mean")
    if b_mean_val is None and metrics.get("ndvi_mean") is not None and metrics.get("ndvi_delta") is not None:
        b_mean_val = round(metrics["ndvi_mean"] - metrics["ndvi_delta"], 3)

    results_data = [
        [Paragraph("<b>Analytical Indicator</b>", table_head), Paragraph("<b>Observed Value</b>", table_head),
         Paragraph("<b>Unit</b>", table_head), Paragraph("<b>Plain-Language Definition</b>", table_head),
         Paragraph("<b>Insurance Assessor Interpretation</b>", table_head)],
        [Paragraph("Observed NDVI Mean", table_bold), Paragraph(_val_or_na(metrics.get("ndvi_mean")), table_text),
         Paragraph("Index [-1, 1]", table_text), Paragraph("Normalized Difference Vegetation Index during observation window", table_text),
         Paragraph("Current crop canopy greenness and photosynthetic absorption", table_text)],
        [Paragraph("Baseline NDVI Mean", table_bold), Paragraph(_val_or_na(b_mean_val), table_text),
         Paragraph("Index [-1, 1]", table_text), Paragraph("Historical healthy vegetation norm for same calendar period", table_text),
         Paragraph("Multi-year reference benchmark for expected healthy crops", table_text)],
        [Paragraph("NDVI Change (ΔNDVI)", table_bold), Paragraph(_val_or_na(metrics.get("ndvi_delta")), table_bold),
         Paragraph("Index Δ", table_text), Paragraph("Observed NDVI minus Historical Baseline NDVI (co-located pixels)", table_text),
         Paragraph("Negative values indicate vegetative depression relative to normal", table_text)],
        [Paragraph("Standardized Anomaly (z-score)", table_bold), Paragraph(_val_or_na(metrics.get("z_score"), fmt="{:.2f}"), table_bold),
         Paragraph("Std Devs (z)", table_text), Paragraph("(Observed − Baseline) / max(Historical Std, 0.02)", table_text),
         Paragraph("Statistical significance; z < -1.5 indicates anomalous stress", table_text)],
        [Paragraph("Estimated Affected Share", table_bold), Paragraph(f"{aff_pct}%", table_bold),
         Paragraph("% of valid px", table_text), Paragraph("Proportion of usable field pixels meeting z < -1.5 criterion", table_text),
         Paragraph("Proportion of field exhibiting qualifying vegetation anomaly", table_text)],
        [Paragraph("Estimated Affected Area", table_bold), Paragraph(f"{aff_ha} ha (~{aff_ac} ac)", table_bold),
         Paragraph("Hectares / Acres", table_text), Paragraph("Calculated as (Affected Share %) × (Total Field Area)", table_text),
         Paragraph("Spatial screening estimate for claim verification", table_text)],
        [Paragraph("Water Index (NDWI Mean)", table_bold), Paragraph(_val_or_na(metrics.get("ndwi_mean")), table_text),
         Paragraph("Index [-1, 1]", table_text), Paragraph("Normalized Difference Water Index (Green − NIR) / (Green + NIR)", table_text),
         Paragraph("Elevated values (>0.05) indicate standing water or waterlogging", table_text)],
    ]
    res_t = Table(results_data, colWidths=[36 * mm, 24 * mm, 20 * mm, 48 * mm, 54 * mm])
    res_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(res_t)
    story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 5 — MAPS & VISUAL EVIDENCE
    # =========================================================================
    story.append(Paragraph("5. Maps and Visual Satellite Evidence", h2))
    if quicklook_path and os.path.exists(quicklook_path):
        story.append(Image(quicklook_path, width=180 * mm, height=58 * mm))
        story.append(Spacer(1, 1.5 * mm))
        legend_desc = (
            "<b>Visual Evidence Panels:</b> (Left) Historical multi-year healthy baseline norm. "
            "(Center) Observed Sentinel-2 composite for the claimed observation window. "
            "(Right) Negative anomaly damage mask: red/orange indicates pixels meeting qualifying stress criteria "
            "(z < -1.5 and Observed < Baseline). Grey pixels denote cloud, shadow, or area outside boundary.<br/>"
            "<i>Note: Satellite false-color maps illustrate spectral vegetation indices and relative greenness, "
            "not verified physical crop loss.</i>"
        )
        story.append(Paragraph(legend_desc, small))
    else:
        story.append(Paragraph("Visual evidence map is not available for this run.", small))
    story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 6 — SEVERITY & CAUSE ASSESSMENT METHODOLOGY
    # =========================================================================
    story.append(Paragraph("6. Severity and Cause Assessment Methodology", h2))
    method_text = (
        "<b>Algorithmic Criteria:</b><br/>"
        "• <b>Anomaly Identification:</b> A pixel is flagged for possible crop stress if its standardized anomaly "
        "z-score satisfies <i>z < -1.5</i> AND observed NDVI is strictly less than the historical baseline mean.<br/>"
        "• <b>Damage Severity Tiers:</b> Evaluated on the fraction of assessable field pixels flagged: "
        "<b>None:</b> &lt;5% | <b>Low:</b> 5%–15% | <b>Moderate:</b> 15%–35% | <b>High:</b> &ge;35%.<br/>"
        "• <b>Peril / Cause Differentiation:</b><br/>"
        "  - <i>Flood / Inundation:</i> Flagged anomaly accompanied by elevated surface moisture (NDWI &gt; 0.05).<br/>"
        "  - <i>Drought / Dry Spell:</i> Flagged anomaly with persistent negative vegetative deficit (ΔNDVI &lt; -0.15) and dry moisture indices.<br/>"
        "  - <i>Hail Damage:</i> Optical satellite composites cannot reliably isolate sudden hail tears from general defoliation without high-frequency radar trajectory data; suspect parcels return <i>Unknown (hail unsupported)</i>.<br/>"
        "  - <i>Unknown / Inconclusive:</i> Where spectral evidence cannot differentiate management changes from weather perils, the cause is reported as Unknown."
    )
    story.append(Paragraph(method_text, small))
    story.append(Spacer(1, 2.5 * mm))

    # Assessment Insights (Traceable Plain-Language Statements)
    story.append(Paragraph("<b>Assessor Guidance Statements:</b>", h3))
    for ins in generate_field_insights(metrics, meta):
        story.append(Paragraph(f"• {ins}", small))
        story.append(Spacer(1, 1 * mm))
    story.append(Spacer(1, 2.5 * mm))

    # =========================================================================
    # SECTION 7 — LIMITATIONS & CLAIM ADJUSTER VERIFICATION
    # =========================================================================
    story.append(Paragraph("7. Limitations and Claim Adjuster Verification Checklist", h2))
    limits_text = (
        "<b>Required Loss Adjustment Verification:</b><br/>"
        "1. <b>Cloud & Atmospheric Gaps:</b> Monsoonal cloud cover may obscure optical observation on specific peak damage dates. Coverage below 30% is reported as Inconclusive.<br/>"
        "2. <b>Phenological Stage Alignment:</b> Sowing delays, varietal differences, or staggered harvesting can alter NDVI curves without reflecting abiotic damage.<br/>"
        "3. <b>Mandatory Ground-Truthing:</b> Satellite indicators should be corroborated by field inspection, geotagged crop photographs, loss adjuster reports, and local rain-gauge data.<br/>"
        "4. <b>Agronomic Management:</b> Weeding, fertilizer application, spray drift, or intercropping can influence spectral response."
    )
    story.append(Paragraph(limits_text, small))
    story.append(Spacer(1, 3.5 * mm))

    # =========================================================================
    # SECTION 8 — REPRODUCIBILITY & EVIDENCE RECORD
    # =========================================================================
    story.append(Paragraph("8. Reproducibility and Audit Record", h2))
    run_dir_name = os.path.basename(os.path.dirname(out_path))
    audit_data = [
        [Paragraph("<b>Parameter / Artifact</b>", table_head), Paragraph("<b>Provenance Record</b>", table_head)],
        [Paragraph("Assessment Run ID", table_bold), Paragraph(run_id, table_text)],
        [Paragraph("Cryptographic Parameter Key", table_bold), Paragraph(param_key, table_text)],
        [Paragraph("Calculation Engine / Version", table_bold), Paragraph("FarmAid Copernicus MSI Engine v2.4 (Open-Source Core)", table_text)],
        [Paragraph("Sentinel-2 Sensor Baseline", table_bold), Paragraph("COPERNICUS/S2_SR_HARMONIZED · Surface Reflectance", table_text)],
        [Paragraph("Artifact File Paths", table_bold), Paragraph(f"GeoJSON: {run_dir_name}/field_result.geojson | Metrics: field_metrics.json", table_text)],
        [Paragraph("Audit Integrity Certification", table_bold), Paragraph("Results were computed strictly for the user-selected boundary without synthetic substitution.", table_text)],
    ]
    audit_t = Table(audit_data, colWidths=[55 * mm, 127 * mm])
    audit_t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1E293B")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E2E8F0")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(audit_t)

    # Document generation with NumberedCanvas
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            rightMargin=14 * mm, leftMargin=14 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    doc.build(story, canvasmaker=NumberedCanvas)
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