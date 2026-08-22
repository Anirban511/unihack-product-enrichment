"""Single-process entrypoint for a Hugging Face Gradio Space.

A Space exposes exactly one port, and the free tier does not offer the Docker
SDK. A Gradio Space, however, runs an arbitrary Python file and installs Debian
packages from `packages.txt` - which is enough to get Chromium. So the whole
product runs in one container on one URL:

    https://<user>-<space>.hf.space/          Gradio UI
    https://<user>-<space>.hf.space/docs      OpenAPI / Swagger
    https://<user>-<space>.hf.space/v1/...    the REST API, unchanged

Gradio is mounted *into* the FastAPI application rather than the other way
round, so the API keeps its own routes, status codes and streaming responses,
and the UI is just another mount point.

Deliberately named `space_app.py`, not `app.py`: a module named `app` sitting
beside the `app/` package makes `import app.main` ambiguous.
"""
from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import gradio as gr
import pandas as pd

from app.delivery import to_csv
from app.main import app as fastapi_app
from app.pipeline import EnrichmentInput, enrich

OUT_DIR = os.environ.get("GRADIO_OUT_DIR", "/tmp/unihack-out")
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _status_markdown(result) -> str:
    verdict = ("🟠 **Flagged for human review**" if result.needs_human_review
               else "🟢 **Accepted** — manufacturer-sourced and fully grounded")
    m = result.metrics
    lines = [
        "### {}   ·   confidence **{:.0%}**".format(verdict, result.confidence),
        "",
        "| | |",
        "|---|---|",
        "| Source tier | {} |".format(
            {1: "1 — manufacturer's own site", 2: "2 — distributor fallback"}
            .get(m.get("source_tier"), "none")),
        "| Columns filled | {} / {} |".format(m.get("delivery_columns_filled", 0),
                                              m.get("delivery_columns_total", 252)),
        "| Evidence units | {} |".format(m.get("evidence", {}).get("evidence_units", 0)),
        "| Live LLM calls | {} |".format(m.get("llm", {}).get("live_calls", 0)),
        "| Elapsed | {:.1f} s |".format(m.get("elapsed_ms", 0) / 1000.0),
    ]
    if result.warnings:
        lines += ["", "**Warnings**", ""] + ["- " + w for w in result.warnings]
    return "\n".join(lines)


def _identity_df(row: Dict[str, str]) -> pd.DataFrame:
    keys = ["MANUFACTURER_NAME", "BRAND_NAME", "MANUFACTURER_PART_NUMBER",
            "Product Name", "Classpath", "Dept", "Class", "Fine", "With", "MFR URL"]
    return pd.DataFrame({"Field": keys, "Value": [row.get(k, "") for k in keys]})


def _descriptions_df(row: Dict[str, str]) -> pd.DataFrame:
    limits = {"INVOICE_DESC": (None, 40), "MOBILE_DESC": (60, 80),
              "SHORT_DESC": (None, 250), "LONG_DESC1": (None, 2000),
              "RETAIL_DESC": (None, 250)}
    recs = []
    for name, (lo, hi) in limits.items():
        text = row.get(name, "") or ""
        ok = len(text) <= hi and (lo is None or not text or len(text) >= lo)
        if name == "INVOICE_DESC" and text and text != text.upper():
            ok = False
        recs.append({"Field": name, "Chars": len(text),
                     "Limit": "{}-{}".format(lo, hi) if lo else "<= {}".format(hi),
                     "OK": "yes" if ok else "NO", "Text": text})
    return pd.DataFrame(recs)


def _attributes_df(row: Dict[str, str]) -> pd.DataFrame:
    recs = []
    for i in range(1, 51):
        label = row.get("ATTRIBUTE_LABEL {}".format(i), "")
        if not label:
            continue
        recs.append({"#": i, "Attribute": label,
                     "Value": row.get("ATTRIBUTE_VALUE {}".format(i), ""),
                     "UOM": row.get("ATTRIBUTE_UOM {}".format(i), "")})
    return pd.DataFrame(recs or [{"#": "", "Attribute": "(none resolved)",
                                  "Value": "", "UOM": ""}])


def _provenance_df(result) -> pd.DataFrame:
    recs = [{"Field": p.field, "Value": p.value[:90], "How": p.method,
             "Tier": p.tier, "New LOV": "yes" if p.is_new_lov_value else "",
             "Source": p.source_url} for p in result.provenance]
    return pd.DataFrame(recs or [{"Field": "(no citations)", "Value": "", "How": "",
                                  "Tier": "", "New LOV": "", "Source": ""}])


def _sources_df(result) -> pd.DataFrame:
    recs = [{"Tier": s.get("tier"), "Policy": s.get("policy", ""),
             "HTTP": s.get("status"), "Via": s.get("fetch_tier", "-"),
             "Why": s.get("reason", ""),
             "Skipped": s.get("skipped", "") or s.get("error", ""),
             "URL": s.get("final_url") or s.get("url", "")}
            for s in result.sources]
    return pd.DataFrame(recs or [{"Tier": "", "Policy": "", "HTTP": "", "Via": "",
                                  "Why": "(no sources)", "Skipped": "", "URL": ""}])


def _rejections_df(result) -> pd.DataFrame:
    rej = result.metrics.get("grounding_rejections", []) or []
    return pd.DataFrame(rej or [{"field": "(nothing rejected)", "value": "",
                                 "reason": "every candidate value was re-found in a source"}])


def _features_markdown(row: Dict[str, str]) -> str:
    parts = []
    if row.get("MARKETING_DESCRIPTION"):
        parts += ["**Marketing copy, verbatim from the manufacturer**", "",
                  "> " + row["MARKETING_DESCRIPTION"], ""]
    feats = [row.get("ITEM_FEATURES_{}".format(i), "") for i in range(1, 21)]
    feats = [f for f in feats if f]
    if feats:
        parts += ["**Item features, verbatim**", ""] + ["- " + f for f in feats]
    return "\n".join(parts) or "_No verbatim marketing copy or feature bullets were found._"


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
def run_single(mpn: str, desc: str, manuf: str, brand: str, sku: str,
               progress=gr.Progress()):
    mpn = (mpn or "").strip()
    if not mpn:
        empty = pd.DataFrame()
        return ("### ⚠️ A manufacturer part number is required.",
                empty, empty, empty, "", empty, empty, empty, None, None)

    progress(0.1, desc="Discovering the manufacturer…")
    result = enrich(EnrichmentInput(Mfg_Part_Num=mpn, Part_Desc=desc or "",
                                    Part_Manuf=manuf or "", E1_Brand=brand or "",
                                    SKU=sku or ""))
    progress(0.9, desc="Writing the delivery row…")
    row = result.delivery_row

    csv_path = os.path.join(OUT_DIR, "delivery_{}.csv".format(mpn.replace("/", "_")))
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(to_csv([row]))

    json_path = os.path.join(OUT_DIR, "result_{}.json".format(mpn.replace("/", "_")))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"input": result.input, "confidence": result.confidence,
                   "needs_human_review": result.needs_human_review,
                   "warnings": result.warnings, "delivery_row": row,
                   "provenance": [p.__dict__ for p in result.provenance],
                   "sources": result.sources, "metrics": result.metrics},
                  fh, indent=2, ensure_ascii=False)

    return (_status_markdown(result), _identity_df(row), _descriptions_df(row),
            _attributes_df(row), _features_markdown(row), _provenance_df(result),
            _sources_df(result), _rejections_df(result), csv_path, json_path)


def run_batch(file_obj, limit: int, offset: int, progress=gr.Progress()):
    if file_obj is None:
        return pd.DataFrame([{"error": "Upload a CSV with a Mfg_Part_Num column."}]), None
    frame = pd.read_csv(file_obj.name, dtype=str, keep_default_na=False)
    if "Mfg_Part_Num" not in frame.columns:
        return pd.DataFrame([{"error": "CSV needs a Mfg_Part_Num column. Found: "
                              + ", ".join(frame.columns[:8])}]), None

    window = frame.iloc[int(offset): int(offset) + int(limit)]
    rows, summary = [], []
    for n, (_i, r) in enumerate(window.iterrows(), start=1):
        progress(n / max(1, len(window)),
                 desc="{}/{}  {}".format(n, len(window), r.get("Mfg_Part_Num", "")))
        try:
            res = enrich(EnrichmentInput(
                Mfg_Part_Num=r.get("Mfg_Part_Num", ""), Part_Desc=r.get("Part_Desc", ""),
                E1_Brand=r.get("E1_Brand", ""), Unilog_Brand=r.get("Unilog_Brand", ""),
                DIB_Brand=r.get("DIB_Brand", ""), Part_Manuf=r.get("Part_Manuf", "")))
            rows.append(res.delivery_row)
            summary.append({"Part": r.get("Mfg_Part_Num", ""),
                            "Brand": res.delivery_row.get("BRAND_NAME", ""),
                            "Classpath": res.delivery_row.get("Classpath", "")[:60],
                            "Short desc": res.delivery_row.get("SHORT_DESC", "")[:70],
                            "Conf": round(res.confidence, 2),
                            "Review": "yes" if res.needs_human_review else "",
                            "Tier": res.metrics.get("source_tier")})
        except Exception as exc:
            summary.append({"Part": r.get("Mfg_Part_Num", ""), "Brand": "",
                            "Classpath": "", "Short desc": "ERROR: {}".format(exc)[:70],
                            "Conf": 0.0, "Review": "yes", "Tier": ""})
    if not rows:
        return pd.DataFrame(summary), None
    path = os.path.join(OUT_DIR, "delivery_format.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(to_csv(rows))
    return pd.DataFrame(summary), path


def reference_status() -> str:
    from app.acquire import fetcher
    from app.extract import llm
    from app.reference.brands import BRANDS
    from app.reference.lov import LOV
    from app.reference.taxonomy import TAXONOMY
    from app.reference.uom import UOM
    return json.dumps({"taxonomy": TAXONOMY.status(), "lov": LOV.status(),
                       "uom": UOM.status(), "brands": BRANDS.status(),
                       "llm": llm.stats(), "runtime": fetcher.cache_stats()},
                      indent=2)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
INTRO = """
# Unilog Product Enrichment Pipeline

Part number in → a standardised, **source-cited** 252-column product record out.

Every emitted value is re-verified against the document it came from before it
is written. Anything that cannot be proven is left **blank**, never guessed.

`/docs` on this same URL serves the REST API.
"""

with gr.Blocks(title="Unilog Product Enrichment", theme=gr.themes.Soft()) as demo:
    gr.Markdown(INTRO)

    with gr.Tab("Enrich a part"):
        with gr.Row():
            in_mpn = gr.Textbox(label="Manufacturer part number *", value="PDSH4816AF")
            in_sku = gr.Textbox(label="Your SKU", value="1515863")
        in_desc = gr.Textbox(label="Distributor description",
                             value="PDSH4816AF Dishwasher SS - Display Only")
        with gr.Row():
            in_manuf = gr.Textbox(label="Part_Manuf (supplier string)",
                                  value="Appliance Dealers Cooperative (APPDE)")
            in_brand = gr.Textbox(label="Brand field (may be a placeholder)",
                                  value="-- Unbranded --")
        go = gr.Button("Enrich", variant="primary")
        gr.Markdown("_A cold part does real network work and can take 30–90 seconds. "
                    "Repeats are cached and return in seconds._")

        out_status = gr.Markdown()
        with gr.Row():
            dl_csv = gr.File(label="Delivery row (CSV, 252 columns)")
            dl_json = gr.File(label="Full result (JSON, with citations)")

        gr.Markdown("### Identity & classification")
        out_identity = gr.Dataframe(wrap=True)
        gr.Markdown("### The five descriptions")
        out_desc = gr.Dataframe(wrap=True)
        gr.Markdown("### Attributes  \n_Blank means no evidence was found — never a guess._")
        out_attrs = gr.Dataframe(wrap=True)
        out_features = gr.Markdown()

        with gr.Accordion("Where every value came from", open=True):
            out_prov = gr.Dataframe(wrap=True)
        with gr.Accordion("Sources retrieved, and why each was chosen", open=False):
            out_src = gr.Dataframe(wrap=True)
        with gr.Accordion("What the pipeline REFUSED to say", open=False):
            gr.Markdown("These values were produced somewhere in the pipeline and then "
                        "**discarded** because they could not be re-found in a retrieved "
                        "document. This is the anti-hallucination gate doing its job.")
            out_rej = gr.Dataframe(wrap=True)

        go.click(run_single, [in_mpn, in_desc, in_manuf, in_brand, in_sku],
                 [out_status, out_identity, out_desc, out_attrs, out_features,
                  out_prov, out_src, out_rej, dl_csv, dl_json])

    with gr.Tab("Batch / CSV"):
        gr.Markdown("Upload a CSV with a **`Mfg_Part_Num`** column. Optional: "
                    "`Part_Desc`, `Part_Manuf`, `E1_Brand`, `Unilog_Brand`, `DIB_Brand`.")
        in_file = gr.File(label="Input CSV", file_types=[".csv"])
        with gr.Row():
            in_limit = gr.Number(label="Rows to process", value=3, precision=0)
            in_offset = gr.Number(label="Start at row", value=0, precision=0)
        go_batch = gr.Button("Run batch", variant="primary")
        out_batch = gr.Dataframe(wrap=True)
        dl_batch = gr.File(label="Delivery CSV (252 columns)")
        go_batch.click(run_batch, [in_file, in_limit, in_offset], [out_batch, dl_batch])

    with gr.Tab("Reference & policy"):
        go_ref = gr.Button("Load reference status")
        out_ref = gr.Code(language="json")
        go_ref.click(reference_status, None, out_ref)
        gr.Markdown("""
### Sourcing policy
1. **Manufacturer's own domain** — including country and shop sub-brands.
2. **Reputed distributor** — fallback only, and always flagged for review.
3. **Consumer marketplaces** — never, at any tier.

### How hallucination is prevented
1. **Grounding gate** — every value must be re-found in downloaded text
   (normalised containment + numeric equivalence, so `50-1/4 in` verifies
   against a page printing `50.25"`). Failures are dropped and listed.
2. **The model returns indices, not text** — it picks *which* scraped pair
   states an attribute; emitted characters are always scraped characters.
3. **Descriptions are constructed, not generated** — from verified attributes
   using the delivery format's own formulas.
""")

# Gradio mounts into FastAPI, so /v1/* and /docs keep working untouched.
app = gr.mount_gradio_app(fastapi_app, demo, path="/")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)),
                timeout_keep_alive=120)
