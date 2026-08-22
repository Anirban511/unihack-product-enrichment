"""Unseen-row spot check with the *minimum* input the brief guarantees.

The challenge states the pipeline receives "Manufacturer Name and Part Number".
So that is all this harness passes: no description, no brand columns, nothing
that would let the pipeline shortcut its way to an answer. Rows are drawn at
random from the 1,000-row input file, which is reference data only - none of it
has been used to tune anything.

    python -m scripts.spotcheck --rows 10 --seed 7
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings
from app.delivery import to_csv
from app.pipeline import EnrichmentInput, enrich
from app.textnorm import clean

# Columns that merely echo the input back. Counting them as "filled" would
# flatter the result, so completeness is measured on everything else.
ECHOED = {
    "Mfg_Part_Num", "Part_Desc", "E1_Brand", "Unilog_Brand", "DIB_Brand",
    "Part_Manuf", "SKU - MY_PART_NUMBER", "PART_NUMBER",
}


def enriched_fill(row: Dict[str, str]) -> int:
    return sum(1 for k, v in row.items() if k not in ECHOED and clean(v))


def summarise(row: Dict[str, str]) -> Dict[str, object]:
    attrs = sum(1 for i in range(1, 51) if clean(row.get("ATTRIBUTE_VALUE {}".format(i), "")))
    feats = sum(1 for i in range(1, 21) if clean(row.get("ITEM_FEATURES_{}".format(i), "")))
    docs = [k for k in ("Specification Sheet", "Owners/User Manual",
                        "Instruction/Installation Manual", "Warranty Information",
                        "Catalog", "SDS", "Energy Star Guide", "Service Manual")
            if clean(row.get(k, ""))]
    return {
        "brand": clean(row.get("BRAND_NAME", "")),
        "manufacturer": clean(row.get("MANUFACTURER_NAME", "")),
        "product_name": clean(row.get("Product Name", "")),
        "classpath": clean(row.get("Classpath", "")),
        "attributes": attrs,
        "features": feats,
        "has_marketing": bool(clean(row.get("MARKETING_DESCRIPTION", ""))),
        "has_image": bool(clean(row.get("Product Image", ""))),
        "documents": docs,
        "descriptions": sum(1 for d in ("MOBILE_DESC", "INVOICE_DESC", "SHORT_DESC",
                                        "LONG_DESC1", "RETAIL_DESC")
                            if clean(row.get(d, ""))),
        "mfr_url": clean(row.get("MFR URL", "")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=10)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    with settings.sample_input_csv.open(encoding="utf-8-sig", newline="") as fh:
        catalogue = list(csv.DictReader(fh))

    random.seed(args.seed)
    picked = random.sample(catalogue, min(args.rows, len(catalogue)))

    Path(args.out).mkdir(parents=True, exist_ok=True)
    report: List[dict] = []
    delivery_rows: List[Dict[str, str]] = []

    print("Spot check: {} random rows, MINIMUM input only "
          "(part number + manufacturer name)\n".format(len(picked)), flush=True)

    for n, src in enumerate(picked, start=1):
        part = src.get("Mfg_Part_Num", "")
        maker = src.get("Part_Manuf", "")
        print("[{}/{}] {}  ({})".format(n, len(picked), part, maker), flush=True)

        # Deliberately minimal: everything else the file offers is withheld.
        item = EnrichmentInput(Mfg_Part_Num=part, Part_Manuf=maker)

        t0 = time.time()
        try:
            result = enrich(item)
        except Exception as exc:
            print("      FAILED {}: {}".format(type(exc).__name__, str(exc)[:140]), flush=True)
            report.append({"part": part, "supplied_manufacturer": maker,
                           "error": "{}: {}".format(type(exc).__name__, str(exc)[:200])})
            continue

        row = result.delivery_row
        delivery_rows.append(row)
        s = summarise(row)
        entry = {
            "part": part,
            "supplied_manufacturer": maker,
            "withheld_description": src.get("Part_Desc", ""),
            "elapsed_s": round(time.time() - t0, 1),
            "confidence": result.confidence,
            "needs_human_review": result.needs_human_review,
            "source_tier": result.metrics.get("source_tier"),
            "enriched_columns_filled": enriched_fill(row),
            "citations": len(result.provenance),
            "live_llm_calls": result.metrics.get("llm", {}).get("live_calls", 0),
            "warnings": result.warnings,
            **s,
        }
        report.append(entry)

        print("      brand={!r} name={!r}".format(s["brand"], s["product_name"]), flush=True)
        print("      class={!r}".format(s["classpath"][:70]), flush=True)
        print("      attrs={} feats={} desc={}/5 img={} docs={} cols={} cites={} conf={} {:.0f}s"
              .format(s["attributes"], s["features"], s["descriptions"],
                      "Y" if s["has_image"] else "n", len(s["documents"]),
                      entry["enriched_columns_filled"], entry["citations"],
                      result.confidence, entry["elapsed_s"]), flush=True)
        print("      url={}".format(s["mfr_url"][:100] or "(none)"), flush=True)
        print(flush=True)

    ok = [r for r in report if "error" not in r]
    found = [r for r in ok if r["enriched_columns_filled"] >= 8]
    aggregate = {
        "rows_attempted": len(picked),
        "rows_completed": len(ok),
        "rows_with_real_data": len(found),
        "hit_rate": round(len(found) / max(1, len(picked)), 3),
        "mean_enriched_columns": round(
            sum(r["enriched_columns_filled"] for r in ok) / max(1, len(ok)), 1),
        "mean_citations": round(sum(r["citations"] for r in ok) / max(1, len(ok)), 1),
        "mean_confidence": round(sum(r["confidence"] for r in ok) / max(1, len(ok)), 3),
        "mean_seconds": round(sum(r["elapsed_s"] for r in ok) / max(1, len(ok)), 1),
        "total_live_llm_calls": sum(r["live_llm_calls"] for r in ok),
        "tier_1_manufacturer": sum(1 for r in ok if r["source_tier"] == 1),
        "tier_2_distributor": sum(1 for r in ok if r["source_tier"] == 2),
        "no_source": sum(1 for r in ok if not r["source_tier"]),
    }

    Path(args.out, "spotcheck.json").write_text(
        json.dumps({"aggregate": aggregate, "rows": report}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    if delivery_rows:
        Path(args.out, "spotcheck_delivery.csv").write_text(
            to_csv(delivery_rows), encoding="utf-8-sig")

    print("=" * 70)
    print(json.dumps(aggregate, indent=2))
    print("\nwrote {0}/spotcheck.json and {0}/spotcheck_delivery.csv".format(args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
