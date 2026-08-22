"""Streamlit front end for the Unilog enrichment pipeline.

Talks to the FastAPI service over HTTP, so the UI can be hosted anywhere while
the scraping stack (which needs Chromium) stays in its own container.

Configuration, in order of precedence:
  1. the API URL typed in the sidebar
  2. st.secrets["API_BASE_URL"]        (Streamlit Cloud / HF Spaces secrets)
  3. the API_BASE_URL environment variable
"""
from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Unilog Product Enrichment",
                   page_icon="🔧", layout="wide",
                   initial_sidebar_state="expanded")

DEFAULT_TIMEOUT = 900          # a cold part can need several page fetches


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def configured_api_url() -> str:
    try:
        if "API_BASE_URL" in st.secrets:
            return str(st.secrets["API_BASE_URL"]).rstrip("/")
    except Exception:
        pass
    return os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def api_get(base: str, path: str) -> Optional[dict]:
    try:
        r = requests.get(base + path, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.session_state["last_error"] = "{}: {}".format(type(exc).__name__, exc)
        return None


def api_post(base: str, path: str, payload: dict, timeout: int = DEFAULT_TIMEOUT):
    r = requests.post(base + path, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------
def confidence_badge(conf: float, review: bool) -> None:
    c1, c2 = st.columns([1, 3])
    c1.metric("Confidence", "{:.0%}".format(conf))
    if review:
        c2.warning("**Flagged for human review** — see the warnings below for why.")
    else:
        c2.success("**Accepted** — sourced from the manufacturer and fully grounded.")


def show_descriptions(row: Dict[str, str]) -> None:
    limits = {"INVOICE_DESC": (None, 40), "MOBILE_DESC": (60, 80),
              "SHORT_DESC": (None, 250), "LONG_DESC1": (None, 2000),
              "RETAIL_DESC": (None, 250)}
    rows = []
    for name, (lo, hi) in limits.items():
        text = row.get(name, "") or ""
        ok = len(text) <= hi and (lo is None or not text or len(text) >= lo)
        if name == "INVOICE_DESC" and text and text != text.upper():
            ok = False
        rows.append({"Field": name, "Chars": len(text),
                     "Limit": "{}–{}".format(lo, hi) if lo else "≤ {}".format(hi),
                     "OK": "✅" if ok else "❌", "Text": text})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 column_config={"Text": st.column_config.TextColumn(width="large")})


def show_attributes(row: Dict[str, str]) -> None:
    recs = []
    for i in range(1, 51):
        label = row.get("ATTRIBUTE_LABEL {}".format(i), "")
        if not label:
            continue
        recs.append({"#": i, "Attribute": label,
                     "Value": row.get("ATTRIBUTE_VALUE {}".format(i), ""),
                     "UOM": row.get("ATTRIBUTE_UOM {}".format(i), "")})
    if not recs:
        st.info("No attributes were populated — the category schema could not be "
                "resolved, or no value survived the grounding check.")
        return
    df = pd.DataFrame(recs)
    filled = int((df["Value"].astype(str).str.strip() != "").sum())
    st.caption("{} of {} attribute slots populated. Blank means *no evidence found* — "
               "never a guess.".format(filled, len(df)))
    st.dataframe(df, use_container_width=True, hide_index=True)


def show_provenance(prov: List[dict]) -> None:
    if not prov:
        st.info("No field-level citations were produced for this part.")
        return
    df = pd.DataFrame(prov)
    keep = [c for c in ["field", "value", "method", "confidence", "tier",
                        "is_new_lov_value", "source_url"] if c in df.columns]
    df = df[keep].rename(columns={
        "field": "Field", "value": "Value", "method": "How it was obtained",
        "confidence": "Match", "tier": "Tier", "is_new_lov_value": "New LOV value",
        "source_url": "Source"})
    st.dataframe(df, use_container_width=True, hide_index=True,
                 column_config={"Source": st.column_config.LinkColumn("Source",
                                                                     display_text="open ↗")})


def show_sources(sources: List[dict]) -> None:
    if not sources:
        st.info("No sources were retrieved.")
        return
    recs = []
    for s in sources:
        recs.append({
            "Tier": s.get("tier"),
            "Policy": s.get("policy", ""),
            "HTTP": s.get("status"),
            "Fetched via": s.get("fetch_tier", "-"),
            "Why this URL": s.get("reason", ""),
            "Skipped": s.get("skipped", "") or s.get("error", ""),
            "URL": s.get("final_url") or s.get("url", ""),
        })
    st.dataframe(pd.DataFrame(recs), use_container_width=True, hide_index=True,
                 column_config={"URL": st.column_config.LinkColumn("URL",
                                                                   display_text="open ↗")})


def show_rejections(metrics: dict) -> None:
    rej = metrics.get("grounding_rejections", []) or []
    if not rej:
        st.success("Nothing was rejected — every candidate value was re-found in a source.")
        return
    st.caption("These values were produced somewhere in the pipeline and then **discarded** "
               "because they could not be re-found in a retrieved document. "
               "This list is the anti-hallucination guarantee doing its job.")
    st.dataframe(pd.DataFrame(rej), use_container_width=True, hide_index=True)


def download_row(row: Dict[str, str], part: str) -> None:
    df = pd.DataFrame([row])
    csv = df.to_csv(index=False).encode("utf-8-sig")
    c1, c2 = st.columns(2)
    c1.download_button("⬇ Delivery row (CSV, 252 columns)", csv,
                       file_name="delivery_{}.csv".format(part or "row"),
                       mime="text/csv", use_container_width=True)
    c2.download_button("⬇ Full result (JSON, with citations)",
                       json.dumps(st.session_state.get("last_result", {}),
                                  indent=2, ensure_ascii=False).encode("utf-8"),
                       file_name="result_{}.json".format(part or "row"),
                       mime="application/json", use_container_width=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
st.sidebar.title("🔧 Unilog Enrichment")
st.sidebar.caption("Part number in → standardised, source-cited product record out.")

api_base = st.sidebar.text_input(
    "API base URL", value=st.session_state.get("api_base", configured_api_url()),
    help="The FastAPI service. On Hugging Face this is your Space URL, e.g. "
         "https://YOURNAME-unihack-api.hf.space")
st.session_state["api_base"] = api_base.rstrip("/")
api_base = st.session_state["api_base"]

if st.sidebar.button("Check connection", use_container_width=True):
    st.session_state["health"] = api_get(api_base, "/v1/health")

health = st.session_state.get("health")
if health:
    st.sidebar.success("API reachable")
    st.sidebar.caption("Model: `{}`".format(health.get("llm", {}).get("model", "?")))
    browser = health.get("http_cache", {}).get("browser_available")
    st.sidebar.caption("Browser tier: {}".format("✅ available" if browser else
                                                 "⚠️ unavailable (HTTP only)"))
elif "last_error" in st.session_state:
    st.sidebar.error("Not reachable")
    st.sidebar.caption(st.session_state["last_error"][:180])
else:
    st.sidebar.info("Press **Check connection** to verify the API.")

st.sidebar.divider()
st.sidebar.markdown(
    "**Sourcing policy**\n\n"
    "1. Manufacturer's own domain\n"
    "2. Reputed distributor *(fallback, always flagged)*\n"
    "3. Consumer marketplaces — **never**")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
st.title("Product Enrichment Pipeline")
st.caption("Every emitted value is re-verified against the document it came from "
           "before it is written. Anything that cannot be proven is left blank.")

tab_single, tab_batch, tab_ref = st.tabs(
    ["Enrich a part", "Batch / CSV", "Reference & policy"])

# --- single ----------------------------------------------------------------
with tab_single:
    with st.form("single"):
        c1, c2 = st.columns(2)
        mpn = c1.text_input("Manufacturer part number *", value="PDSH4816AF")
        sku = c2.text_input("Your SKU", value="")
        desc = st.text_input("Distributor description",
                             value="PDSH4816AF Dishwasher SS - Display Only")
        c3, c4 = st.columns(2)
        manuf = c3.text_input("Part_Manuf (supplier string)",
                              value="Appliance Dealers Cooperative (APPDE)")
        brand = c4.text_input("Brand field (may be a placeholder)",
                              value="-- Unbranded --")
        submitted = st.form_submit_button("Enrich", type="primary",
                                          use_container_width=True)

    if submitted:
        if not mpn.strip():
            st.error("A manufacturer part number is required.")
        else:
            payload = {"Mfg_Part_Num": mpn.strip(), "Part_Desc": desc,
                       "Part_Manuf": manuf, "E1_Brand": brand, "SKU": sku}
            with st.spinner("Discovering the manufacturer, fetching pages and PDFs, "
                            "verifying every value… this can take a minute."):
                try:
                    st.session_state["last_result"] = api_post(api_base, "/v1/enrich", payload)
                except Exception as exc:
                    st.session_state["last_result"] = None
                    st.error("Request failed — {}: {}".format(type(exc).__name__, exc))

    result = st.session_state.get("last_result")
    if result:
        row = result.get("delivery_row", {})
        confidence_badge(result.get("confidence", 0.0),
                         result.get("needs_human_review", True))

        m = result.get("metrics", {})
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("Columns filled", "{} / {}".format(m.get("delivery_columns_filled", 0),
                                                     m.get("delivery_columns_total", 252)))
        k2.metric("Evidence units", m.get("evidence", {}).get("evidence_units", 0))
        k3.metric("Live LLM calls", m.get("llm", {}).get("live_calls", 0))
        k4.metric("Source tier", m.get("source_tier", "—"))

        for w in result.get("warnings", []):
            st.warning(w)

        st.subheader("Identity & classification")
        ident = {k: row.get(k, "") for k in
                 ["MANUFACTURER_NAME", "BRAND_NAME", "MANUFACTURER_PART_NUMBER",
                  "Product Name", "Classpath", "Dept", "Class", "Fine", "With"]}
        st.dataframe(pd.DataFrame([ident]).T.rename(columns={0: "Value"}),
                     use_container_width=True)
        if row.get("MFR URL"):
            st.markdown("**Manufacturer page:** [{}]({})".format(row["MFR URL"][:110],
                                                                 row["MFR URL"]))

        st.subheader("The five descriptions")
        show_descriptions(row)

        st.subheader("Attributes")
        show_attributes(row)

        st.subheader("Marketing copy & features")
        if row.get("MARKETING_DESCRIPTION"):
            st.info(row["MARKETING_DESCRIPTION"])
        feats = [row.get("ITEM_FEATURES_{}".format(i), "") for i in range(1, 21)]
        feats = [f for f in feats if f]
        if feats:
            st.markdown("\n".join("- " + f for f in feats))
        elif not row.get("MARKETING_DESCRIPTION"):
            st.info("No verbatim marketing copy or feature bullets were found.")

        st.subheader("Where every value came from")
        show_provenance(result.get("provenance", []))

        with st.expander("Sources retrieved (and why each was chosen)"):
            show_sources(result.get("sources", []))
        with st.expander("What the pipeline refused to say"):
            show_rejections(m)
        with st.expander("Full 252-column delivery row"):
            nonblank = {k: v for k, v in row.items() if str(v).strip()}
            st.dataframe(pd.DataFrame([nonblank]).T.rename(columns={0: "Value"}),
                         use_container_width=True)
        with st.expander("Raw metrics"):
            st.json(m)

        st.divider()
        download_row(row, row.get("Mfg_Part_Num", ""))

# --- batch -----------------------------------------------------------------
with tab_batch:
    st.markdown("Upload a CSV with a **`Mfg_Part_Num`** column. Optional columns: "
                "`Part_Desc`, `Part_Manuf`, `E1_Brand`, `Unilog_Brand`, `DIB_Brand`.")
    upload = st.file_uploader("Input CSV", type=["csv"])
    c1, c2, c3 = st.columns(3)
    limit = c1.number_input("Rows to process", 1, 500, 5)
    offset = c2.number_input("Start at row", 0, 100000, 0)
    conc = c3.number_input("Concurrency", 1, 8, 2)

    if upload is not None:
        preview = pd.read_csv(upload)
        upload.seek(0)
        st.caption("{} rows in file. Previewing the first 5.".format(len(preview)))
        st.dataframe(preview.head(), use_container_width=True, hide_index=True)

        if st.button("Run batch", type="primary", use_container_width=True):
            with st.spinner("Enriching {} rows… each row does real network work."
                            .format(limit)):
                try:
                    files = {"file": (upload.name, upload.getvalue(), "text/csv")}
                    r = requests.post(
                        "{}/v1/enrich/csv?limit={}&offset={}&concurrency={}".format(
                            api_base, int(limit), int(offset), int(conc)),
                        files=files, timeout=DEFAULT_TIMEOUT * 3)
                    r.raise_for_status()
                    out = pd.read_csv(io.BytesIO(r.content), encoding="utf-8-sig")
                    st.success("Delivered {} of {} requested rows.".format(
                        r.headers.get("X-Rows-Delivered", len(out)),
                        r.headers.get("X-Rows-Requested", limit)))
                    show_cols = [c for c in ["Mfg_Part_Num", "BRAND_NAME", "Classpath",
                                             "Product Name", "SHORT_DESC", "MFR URL"]
                                 if c in out.columns]
                    st.dataframe(out[show_cols], use_container_width=True, hide_index=True)
                    st.download_button("⬇ Delivery CSV (252 columns)", r.content,
                                       file_name="delivery_format.csv", mime="text/csv",
                                       use_container_width=True)
                except Exception as exc:
                    st.error("Batch failed — {}: {}".format(type(exc).__name__, exc))

# --- reference -------------------------------------------------------------
with tab_ref:
    if st.button("Load reference status", use_container_width=True):
        st.session_state["reference"] = api_get(api_base, "/v1/reference")
    ref = st.session_state.get("reference")
    if ref:
        st.caption(ref.get("note", ""))
        for key in ("taxonomy", "lov", "uom", "brands"):
            if key in ref:
                st.markdown("**{}**".format(key.upper()))
                st.json(ref[key])
        st.metric("Delivery columns", ref.get("delivery_columns", 0))
    else:
        st.info("Press the button to read which reference tables the API has loaded.")

    st.divider()
    st.markdown(
        "#### How hallucination is prevented\n"
        "1. **Grounding gate** — every value must be re-found in downloaded text "
        "(normalised containment + numeric equivalence, so `50-1/4 in` verifies "
        "against a page printing `50.25\"`). Failures are dropped and listed.\n"
        "2. **The model returns indices, not text** — it picks *which* scraped pair "
        "states an attribute; the characters written out are always scraped "
        "characters.\n"
        "3. **Descriptions are constructed, not generated** — from verified "
        "attributes using the delivery format's own formulas.")
