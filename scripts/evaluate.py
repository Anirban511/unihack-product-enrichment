"""Field-level accuracy against the labelled delivery-format rows.

The guide asks for exactly this: "Field-level accuracy against the known-good
rows, character-limit compliance, and percentage of values found in the LOV are
all simple, credible metrics. Judges will look for them."

Run:  python -m scripts.evaluate            (both labelled rows)
      python -m scripts.evaluate --sample 5 (5 rows from the 1,000-row input)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz

from app.config import settings
from app.delivery import columns, to_csv
from app.describe import LIMITS
from app.pipeline import EnrichmentInput, enrich
from app.textnorm import clean, norm

# Fields worth scoring. Bookkeeping columns that are copied straight from the
# input would inflate the score, so they are excluded.
SCORED = [
    "Classpath", "Dept", "Class", "Fine", "MANUFACTURER_PART_NUMBER", "BRAND_NAME",
    "Product Name", "MOBILE_DESC", "INVOICE_DESC", "SHORT_DESC", "LONG_DESC1",
    "RETAIL_DESC", "MARKETING_DESCRIPTION", "With", "Standard/Approvals", "Warranty",
    "UPC", "EAN", "UNSPSC", "Product Image", "Specification Sheet",
]
ATTR_SLOTS = 50


def _match(expected: str, got: str) -> Tuple[str, float]:
    e, g = norm(expected), norm(got)
    if not e and not g:
        return "both-blank", 1.0
    if not e:
        return "extra", 0.0
    if not g:
        return "missed", 0.0
    if e == g:
        return "exact", 1.0
    r = fuzz.token_set_ratio(e, g) / 100.0
    return ("near" if r >= 0.85 else "wrong"), r


def score_row(expected: Dict[str, str], got: Dict[str, str]) -> dict:
    field_scores: Dict[str, dict] = {}
    for col in SCORED:
        kind, r = _match(expected.get(col, ""), got.get(col, ""))
        field_scores[col] = {"expected": expected.get(col, "")[:120],
                             "got": got.get(col, "")[:120], "verdict": kind,
                             "similarity": round(r, 3)}

    # Attributes are compared as label->value sets, not by slot index.
    def attrs(row: Dict[str, str]) -> Dict[str, str]:
        out = {}
        for i in range(1, ATTR_SLOTS + 1):
            lab = clean(row.get("ATTRIBUTE_LABEL {}".format(i), ""))
            val = clean(row.get("ATTRIBUTE_VALUE {}".format(i), ""))
            uom = clean(row.get("ATTRIBUTE_UOM {}".format(i), ""))
            if lab:
                out[norm(lab)] = norm((val + " " + uom).strip())
        return out

    exp_a, got_a = attrs(expected), attrs(got)
    exp_filled = {k: v for k, v in exp_a.items() if v}
    label_hits = len(set(exp_a) & set(got_a))
    value_hits = sum(1 for k, v in exp_filled.items() if got_a.get(k) == v)
    value_near = sum(1 for k, v in exp_filled.items()
                     if k in got_a and got_a[k] and fuzz.ratio(v, got_a[k]) >= 85)

    scored_cols = [c for c in SCORED if expected.get(c, "").strip()]
    correct = sum(1 for c in scored_cols if field_scores[c]["verdict"] in ("exact", "near"))

    return {
        "fields": field_scores,
        "field_accuracy": round(correct / len(scored_cols), 3) if scored_cols else 0.0,
        "fields_scored": len(scored_cols),
        "fields_correct": correct,
        "attributes": {
            "expected_labels": len(exp_a), "label_recall":
                round(label_hits / len(exp_a), 3) if exp_a else 0.0,
            "expected_values": len(exp_filled),
            "value_exact": value_hits,
            "value_near": value_near,
            "value_recall": round(value_near / len(exp_filled), 3) if exp_filled else 0.0,
        },
    }


def char_limit_report(row: Dict[str, str]) -> dict:
    out = {}
    for name, (lo, hi) in LIMITS.items():
        text = row.get(name, "")
        ok = len(text) <= hi and (lo is None or not text or len(text) >= lo)
        if name == "INVOICE_DESC" and text and text != text.upper():
            ok = False
        out[name] = {"length": len(text), "limit": [lo, hi], "compliant": ok}
    return out


def load_truth() -> List[Dict[str, str]]:
    with settings.delivery_format_csv.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def load_inputs() -> List[Dict[str, str]]:
    with settings.sample_input_csv.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="also run N rows from the unlabelled input file")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    Path(args.out).mkdir(parents=True, exist_ok=True)
    report: Dict[str, object] = {"labelled": [], "sample": []}
    produced: List[Dict[str, str]] = []

    for truth in load_truth():
        item = EnrichmentInput(
            Mfg_Part_Num=truth.get("Mfg_Part_Num", ""),
            Part_Desc=truth.get("Part_Desc", ""),
            E1_Brand=truth.get("E1_Brand", ""),
            Unilog_Brand=truth.get("Unilog_Brand", ""),
            DIB_Brand=truth.get("DIB_Brand", ""),
            Part_Manuf=truth.get("Part_Manuf", ""),
            SKU=truth.get("SKU - MY_PART_NUMBER", ""),
        )
        print("[labelled] {} ...".format(item.Mfg_Part_Num), flush=True)
        result = enrich(item)
        produced.append(result.delivery_row)
        entry = {
            "part": item.Mfg_Part_Num,
            "confidence": result.confidence,
            "needs_human_review": result.needs_human_review,
            "warnings": result.warnings,
            "source_tier": result.metrics.get("source_tier"),
            "llm": result.metrics.get("llm"),
            "elapsed_ms": result.metrics.get("elapsed_ms"),
            "grounding_rejections": len(result.metrics.get("grounding_rejections", [])),
            "char_limits": char_limit_report(result.delivery_row),
            "score": score_row(truth, result.delivery_row),
        }
        report["labelled"].append(entry)
        print("   field accuracy {:.0%}  attr value recall {:.0%}  conf {:.2f}".format(
            entry["score"]["field_accuracy"],
            entry["score"]["attributes"]["value_recall"], result.confidence), flush=True)

    for row in load_inputs()[args.offset: args.offset + args.sample]:
        item = EnrichmentInput(
            Mfg_Part_Num=row.get("Mfg_Part_Num", ""), Part_Desc=row.get("Part_Desc", ""),
            E1_Brand=row.get("E1_Brand", ""), Unilog_Brand=row.get("Unilog_Brand", ""),
            DIB_Brand=row.get("DIB_Brand", ""), Part_Manuf=row.get("Part_Manuf", ""))
        print("[sample] {} ...".format(item.Mfg_Part_Num), flush=True)
        try:
            result = enrich(item)
        except Exception as exc:
            report["sample"].append({"part": item.Mfg_Part_Num, "error": str(exc)[:200]})
            continue
        produced.append(result.delivery_row)
        report["sample"].append({
            "part": item.Mfg_Part_Num,
            "classpath": result.delivery_row.get("Classpath", ""),
            "brand": result.delivery_row.get("BRAND_NAME", ""),
            "product_name": result.delivery_row.get("Product Name", ""),
            "short_desc": result.delivery_row.get("SHORT_DESC", ""),
            "confidence": result.confidence,
            "needs_human_review": result.needs_human_review,
            "source_tier": result.metrics.get("source_tier"),
            "columns_filled": result.metrics.get("delivery_columns_filled"),
            "attributes_populated": result.metrics.get("attributes", {}).get("populated"),
            "llm": result.metrics.get("llm"),
            "warnings": result.warnings,
        })

    labelled = report["labelled"]
    if labelled:
        report["summary"] = {
            "rows": len(labelled),
            "mean_field_accuracy": round(
                sum(e["score"]["field_accuracy"] for e in labelled) / len(labelled), 3),
            "mean_attribute_label_recall": round(
                sum(e["score"]["attributes"]["label_recall"] for e in labelled) / len(labelled), 3),
            "mean_attribute_value_recall": round(
                sum(e["score"]["attributes"]["value_recall"] for e in labelled) / len(labelled), 3),
            "char_limit_compliance": round(sum(
                sum(1 for v in e["char_limits"].values() if v["compliant"]) / len(e["char_limits"])
                for e in labelled) / len(labelled), 3),
            "mean_confidence": round(sum(e["confidence"] for e in labelled) / len(labelled), 3),
            "total_live_llm_calls": sum(e["llm"]["live_calls"] for e in labelled),
            "total_llm_tokens": sum(e["llm"]["prompt_tokens"] + e["llm"]["completion_tokens"]
                                    for e in labelled),
        }

    Path(args.out, "evaluation.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    Path(args.out, "delivery_output.csv").write_text(to_csv(produced), encoding="utf-8-sig")
    print("\n" + json.dumps(report.get("summary", {}), indent=2))
    print("\nwrote {}/evaluation.json and {}/delivery_output.csv".format(args.out, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
