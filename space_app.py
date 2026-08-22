"""Product enrichment dashboard.

Two inputs go in - manufacturer name and part number. A complete, standardised,
source-cited product record comes out, in the client's delivery format.

The REST API is mounted alongside for programmatic use, but the dashboard is a
working tool, not documentation for it.

Deliberately named `space_app.py`, not `app.py`: a module named `app` sitting
beside the `app/` package makes `import app.main` ambiguous.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Tuple

import gradio as gr
import pandas as pd

from app.delivery import to_csv
from app.main import app as fastapi_app
from app.pipeline import EnrichmentInput, enrich
from app.textnorm import clean

OUT_DIR = os.environ.get("GRADIO_OUT_DIR", "/tmp/unihack-out")
os.makedirs(OUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# ZeroGPU compatibility
# ---------------------------------------------------------------------------
# The pipeline is CPU work: HTTP fetches, a headless browser, PDF text
# extraction and a remote LLM call. A ZeroGPU Space refuses to start without a
# `@spaces.GPU` entry point, so register one and never call it. Off-platform
# `spaces` is absent and this is a no-op.
try:                                              # pragma: no cover - platform shim
    import spaces

    @spaces.GPU(duration=5)
    def gpu_probe() -> str:
        return "ok"

    ZEROGPU = True
except Exception:
    gpu_probe = None
    ZEROGPU = False


DESCRIPTION_FIELDS = [
    ("MOBILE_DESC", "Mobile"),
    ("INVOICE_DESC", "Invoice"),
    ("SHORT_DESC", "Short"),
    ("LONG_DESC1", "Long"),
    ("RETAIL_DESC", "Retail"),
]

IDENTITY_FIELDS = [
    ("MANUFACTURER_NAME", "Manufacturer"),
    ("BRAND_NAME", "Brand"),
    ("MANUFACTURER_PART_NUMBER", "Part number"),
    ("Product Name", "Product name"),
    ("Classpath", "Category"),
    ("Dept", "Department"),
    ("Class", "Class"),
    ("Fine", "Fine"),
]

COMMERCE_FIELDS = [
    ("UPC", "UPC"), ("EAN", "EAN"), ("GTIN", "GTIN"), ("UNSPSC", "UNSPSC"),
    ("Warranty", "Warranty"), ("List Price", "List price"),
    ("Selling Qty", "Selling qty"), ("Selling UOM", "Selling UOM"),
    ("Country Of Origin", "Country of origin"),
    ("Standard Packaging Information", "Packaging"),
    ("LENGTH", "Length"), ("WIDTH", "Width"), ("HEIGHT", "Height"),
    ("WEIGHT", "Weight"), ("Standard/Approvals", "Standards & approvals"),
]

DOCUMENT_FIELDS = [
    "Specification Sheet", "Owners/User Manual", "Instruction/Installation Manual",
    "Service Manual", "Warranty Information", "Catalog", "SDS", "Energy Star Guide",
    "Line Drawing", "Submittal", "Technical Bulletin", "Size Chart",
    "Compatibility Chart", "RoHS", "MTR",
]

EMPTY = pd.DataFrame()


# ---------------------------------------------------------------------------
# Output shaping
# ---------------------------------------------------------------------------
def _headline(result, row: Dict[str, str]) -> str:
    if not result.metrics.get("source_tier"):
        return ("### No product data found\n\n"
                "Nothing could be retrieved from the manufacturer's website for this "
                "part. Check the part number, or try adding the manufacturer name.")
    name = " ".join(x for x in [clean(row.get("BRAND_NAME", "")),
                                clean(row.get("MANUFACTURER_PART_NUMBER", "")),
                                clean(row.get("Product Name", ""))] if x)
    flag = ("&nbsp;&nbsp;·&nbsp;&nbsp;needs review" if result.needs_human_review else "")
    return "### {}\n\nConfidence **{:.0%}**{}".format(
        name or clean(row.get("Mfg_Part_Num", "")), result.confidence, flag)


def _pairs_df(row: Dict[str, str], fields) -> pd.DataFrame:
    recs = [{"Field": label, "Value": clean(row.get(key, ""))}
            for key, label in fields if clean(row.get(key, ""))]
    return pd.DataFrame(recs or [{"Field": "—", "Value": "not found"}])


def _descriptions_df(row: Dict[str, str]) -> pd.DataFrame:
    recs = [{"Type": label, "Description": clean(row.get(key, ""))}
            for key, label in DESCRIPTION_FIELDS if clean(row.get(key, ""))]
    return pd.DataFrame(recs or [{"Type": "—", "Description": "not generated"}])


def _attributes_df(row: Dict[str, str], sources: Dict[str, str]) -> pd.DataFrame:
    recs = []
    for i in range(1, 51):
        label = clean(row.get("ATTRIBUTE_LABEL {}".format(i), ""))
        value = clean(row.get("ATTRIBUTE_VALUE {}".format(i), ""))
        if not label or not value:
            continue
        recs.append({"Attribute": label, "Value": value,
                     "Unit": clean(row.get("ATTRIBUTE_UOM {}".format(i), "")),
                     "Source": sources.get("ATTRIBUTE:" + label, "")})
    return pd.DataFrame(recs or [{"Attribute": "—", "Value": "no attributes found",
                                  "Unit": "", "Source": ""}])


def _content_markdown(row: Dict[str, str]) -> str:
    parts: List[str] = []
    marketing = clean(row.get("MARKETING_DESCRIPTION", ""))
    if marketing:
        parts += ["**Marketing description**", "", marketing, ""]
    feats = [clean(row.get("ITEM_FEATURES_{}".format(i), "")) for i in range(1, 21)]
    feats = [f for f in feats if f]
    if feats:
        parts += ["**Features**", ""] + ["- " + f for f in feats]
    return "\n".join(parts) or "_No marketing copy or features found on the manufacturer's site._"


def _assets_df(row: Dict[str, str], sources: Dict[str, str]) -> pd.DataFrame:
    recs = []
    for key, kind in [("Product Image", "Product image")] + \
                     [("Alternate Image {}".format(i), "Alternate image") for i in range(1, 5)]:
        if clean(row.get(key, "")):
            recs.append({"Asset": kind, "File": clean(row.get(key, "")),
                         "Source": sources.get(key, "")})
    for key in DOCUMENT_FIELDS:
        if clean(row.get(key, "")):
            recs.append({"Asset": key, "File": clean(row.get(key, "")),
                         "Source": sources.get(key, "")})
    if clean(row.get("Video Link", "")):
        recs.append({"Asset": "Video", "File": clean(row.get("Video Link", "")),
                     "Source": clean(row.get("Video Link", ""))})
    return pd.DataFrame(recs or [{"Asset": "—", "File": "no assets found", "Source": ""}])


def _sources_df(result) -> pd.DataFrame:
    recs = [{"Field": p.field.replace("ATTRIBUTE:", ""), "Value": p.value[:80],
             "Source URL": p.source_url} for p in result.provenance]
    return pd.DataFrame(recs or [{"Field": "—", "Value": "", "Source URL": ""}])


def _source_lookup(result) -> Dict[str, str]:
    return {p.field: p.source_url for p in result.provenance}


def _write_files(row: Dict[str, str], result, part: str) -> Tuple[str, str]:
    stem = (part or "record").replace("/", "_").replace("\\", "_")
    csv_path = os.path.join(OUT_DIR, "{}.csv".format(stem))
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(to_csv([row]))
    json_path = os.path.join(OUT_DIR, "{}.json".format(stem))
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump({"product": row,
                   "sources": [{"field": p.field, "value": p.value,
                                "source_url": p.source_url} for p in result.provenance]},
                  fh, indent=2, ensure_ascii=False)
    return csv_path, json_path


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------
def run_single(manufacturer: str, part_number: str, progress=gr.Progress()):
    part_number = (part_number or "").strip()
    if not part_number:
        return ("### Enter a part number to begin.",
                EMPTY, EMPTY, EMPTY, "", EMPTY, EMPTY, None, None)

    progress(0.05, desc="Locating the manufacturer's website…")
    result = enrich(EnrichmentInput(Mfg_Part_Num=part_number,
                                    Part_Manuf=(manufacturer or "").strip()))
    progress(0.95, desc="Building the product record…")

    row = result.delivery_row
    sources = _source_lookup(result)
    csv_path, json_path = _write_files(row, result, part_number)

    return (_headline(result, row),
            _pairs_df(row, IDENTITY_FIELDS),
            _descriptions_df(row),
            _attributes_df(row, sources),
            _content_markdown(row),
            _assets_df(row, sources),
            _pairs_df(row, COMMERCE_FIELDS),
            csv_path, json_path)


def run_batch(file_obj, limit: int, progress=gr.Progress()):
    if file_obj is None:
        return pd.DataFrame([{"Status": "Upload a CSV first."}]), None

    frame = pd.read_csv(file_obj.name, dtype=str, keep_default_na=False)
    cols = {c.strip().lower(): c for c in frame.columns}

    def pick(*names) -> Optional[str]:
        for n in names:
            if n in cols:
                return cols[n]
        return None

    part_col = pick("part number", "part_number", "mfg_part_num", "mpn",
                    "manufacturer part number", "manufacturer_part_number")
    maker_col = pick("manufacturer name", "manufacturer_name", "manufacturer",
                     "part_manuf", "brand")
    if not part_col:
        return pd.DataFrame([{"Status": "No part-number column found. Expected one of: "
                              "Part Number, Mfg_Part_Num, MPN. Found: "
                              + ", ".join(list(frame.columns)[:8])}]), None

    window = frame.head(int(limit))
    rows, summary = [], []
    for n, (_i, r) in enumerate(window.iterrows(), start=1):
        part = clean(r.get(part_col, ""))
        maker = clean(r.get(maker_col, "")) if maker_col else ""
        progress(n / max(1, len(window)), desc="{}/{}  {}".format(n, len(window), part))
        if not part:
            continue
        try:
            res = enrich(EnrichmentInput(Mfg_Part_Num=part, Part_Manuf=maker))
            rows.append(res.delivery_row)
            d = res.delivery_row
            summary.append({
                "Part number": part,
                "Brand": clean(d.get("BRAND_NAME", "")),
                "Product name": clean(d.get("Product Name", "")),
                "Category": clean(d.get("Classpath", ""))[:52],
                "Attributes": sum(1 for i in range(1, 51)
                                  if clean(d.get("ATTRIBUTE_VALUE {}".format(i), ""))),
                "Confidence": round(res.confidence, 2),
                "Review": "yes" if res.needs_human_review else "",
            })
        except Exception as exc:
            summary.append({"Part number": part, "Brand": "", "Product name": "",
                            "Category": "failed: {}".format(type(exc).__name__),
                            "Attributes": 0, "Confidence": 0.0, "Review": "yes"})

    if not rows:
        return pd.DataFrame(summary or [{"Status": "Nothing could be enriched."}]), None

    path = os.path.join(OUT_DIR, "enriched_products.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(to_csv(rows))
    return pd.DataFrame(summary), path


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------
CSS = """
.gradio-container {max-width: 1180px !important;}
footer {display: none !important;}
"""

THEME = gr.themes.Soft(primary_hue="blue", neutral_hue="slate")

with gr.Blocks(title="Product Enrichment", css=CSS) as demo:
    gr.Markdown("# Product Enrichment\n"
                "Enter a manufacturer and part number. The product record is built from "
                "the manufacturer's own website.")

    with gr.Tab("Single product"):
        with gr.Row():
            in_manufacturer = gr.Textbox(label="Manufacturer name", scale=2,
                                         placeholder="e.g. Frigidaire")
            in_part = gr.Textbox(label="Part number", scale=2,
                                 placeholder="e.g. PDSH4816AF")
            go = gr.Button("Enrich", variant="primary", scale=1)

        out_head = gr.Markdown()

        with gr.Row():
            dl_csv = gr.File(label="Download product record (CSV)")
            dl_json = gr.File(label="Download with sources (JSON)")

        gr.Markdown("### Product")
        out_identity = gr.Dataframe(headers=["Field", "Value"], wrap=True,
                                    show_label=False)

        gr.Markdown("### Descriptions")
        out_desc = gr.Dataframe(wrap=True, show_label=False)

        gr.Markdown("### Attributes")
        out_attrs = gr.Dataframe(wrap=True, show_label=False)

        gr.Markdown("### Content")
        out_content = gr.Markdown()

        gr.Markdown("### Digital assets")
        out_assets = gr.Dataframe(wrap=True, show_label=False)

        gr.Markdown("### Commercial data")
        out_commerce = gr.Dataframe(wrap=True, show_label=False)

        go.click(run_single, [in_manufacturer, in_part],
                 [out_head, out_identity, out_desc, out_attrs, out_content,
                  out_assets, out_commerce, dl_csv, dl_json])
        in_part.submit(run_single, [in_manufacturer, in_part],
                       [out_head, out_identity, out_desc, out_attrs, out_content,
                        out_assets, out_commerce, dl_csv, dl_json])

    with gr.Tab("Bulk upload"):
        gr.Markdown("Upload a CSV containing a part-number column, and optionally a "
                    "manufacturer column. Everything else is discovered.")
        in_file = gr.File(label="CSV file", file_types=[".csv"])
        in_limit = gr.Number(label="Products to process", value=5, precision=0)
        go_batch = gr.Button("Enrich all", variant="primary")
        out_batch = gr.Dataframe(wrap=True, show_label=False)
        dl_batch = gr.File(label="Download enriched products (CSV)")
        go_batch.click(run_batch, [in_file, in_limit], [out_batch, dl_batch])

        # Wired in so ZeroGPU detects a GPU entry point at startup. Hidden: it is
        # a platform requirement, not a feature.
        if gpu_probe is not None:
            with gr.Row(visible=False):
                _pb = gr.Button("probe")
                _po = gr.Textbox()
                _pb.click(gpu_probe, None, _po)


# Off-platform we own the process, so Gradio mounts into FastAPI and the REST
# API keeps the root: /docs and /v1/...
app = gr.mount_gradio_app(fastapi_app, demo, path="/")

# On a Space the runtime owns port 7860 before our code runs, so a second server
# cannot bind it. Let Gradio launch, then attach the REST API to the server it
# already started - the API lives under /api there.
ON_SPACE = bool(os.environ.get("SPACE_ID") or os.environ.get("SPACE_REPO_NAME"))

if __name__ == "__main__":
    if ON_SPACE:
        demo.launch(server_name="0.0.0.0", theme=THEME, prevent_thread_lock=True)
        try:
            demo.app.mount("/api", fastapi_app)
            # Starlette matches in registration order and Gradio has already
            # registered a catch-all for its SPA, which would swallow /api/*.
            routes = demo.app.router.routes
            routes.insert(0, routes.pop())
        except Exception as exc:                 # the UI must survive this failing
            print("could not mount the REST API: {}: {}".format(type(exc).__name__, exc))
        import threading
        threading.Event().wait()
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)),
                    timeout_keep_alive=120)
