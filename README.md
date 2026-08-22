---
title: Unilog Enrichment API
emoji: 🔧
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Part number in, source-cited 252-column product record out
---

# Unilog Product Enrichment Pipeline

Manufacturer part number in → a complete, standardised, **source-cited** product
record out, in the client's 252-column delivery format.

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload          # http://127.0.0.1:8000/docs
python -m scripts.evaluate             # field-level accuracy vs the labelled rows
```

`GROQ_API_KEY` lives in `.env`. Chrome must be installed for the browser tier
(the pipeline degrades to HTTP-only if it is not, and says so in `/v1/health`).

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/v1/enrich` | one part → delivery row + provenance + metrics |
| `POST` | `/v1/enrich/batch` | many parts, bounded concurrency |
| `POST` | `/v1/enrich/csv` | upload the input CSV, download the delivery CSV |
| `GET` | `/v1/reference` | which reference tables are loaded and from where |
| `GET` | `/v1/health` | LLM / browser / cache readiness, sourcing policy |

```bash
curl -X POST localhost:8000/v1/enrich -H 'content-type: application/json' -d '{
  "Mfg_Part_Num": "PDSH4816AF",
  "Part_Desc": "PDSH4816AF Dishwasher SS - Display Only",
  "Part_Manuf": "Appliance Dealers Cooperative (APPDE)"
}'
```

## How hallucination is made structurally impossible

Two mechanisms, neither of which is a prompt instruction.

**1. The grounding gate (`app/evidence.py`).** Every candidate value must be
*re-found* in raw text that was actually downloaded, by normalised containment
plus numeric equivalence (so `50-1/4 in` verifies against a page printing
`50.25"`). Values that fail are dropped and listed in
`metrics.grounding_rejections` — the pipeline reports what it refused to say.

**2. The LLM never writes a product fact (`app/extract/select.py`).** It is handed
numbered lists and returns *numbers*: an index into the taxonomy shortlist, the
index of the scraped pair that states an attribute, the indices of genuine
feature bullets. The text written out is always the text that was scraped. An
out-of-range or missing answer falls back to the deterministic path.

The five descriptions are **constructed, not generated** (`app/describe.py`) from
verified attributes using the delivery format's own construction formulas, so a
description cannot contain a fact the pipeline could not prove.

## Sourcing policy

Enforced in `app/acquire/discovery.py`, before anything enters the evidence store.

* **Tier 1** — the manufacturer's own domain, including its country and shop
  sub-brands (`whirlpool.ca` counts as Whirlpool's own site).
* **Tier 2** — reputed industrial distributors, only when tier 1 yields nothing.
  A tier-2 row is always flagged for review.
* **Never** — consumer marketplaces, at any tier.

The supplier string on an input row is usually a *distributor account*
("Appliance Dealers Cooperative (APPDE)"), not the manufacturer. So the part
number leads: search it, drop words the distributor already used (those are
product nouns, not brands), and the token that keeps appearing beside the part
number across independent hosts is the brand. The domain named after that brand
is the manufacturer's. The correctly-cased form (`FRIGIDAIRE®`) is then read off
the manufacturer's own page, never assumed.

## Scraping tiers

1. **HTTP** (httpx) — cheap, handles most pages.
2. **Headless Chrome** (Selenium) — used when the HTTP result is a JS shell *or*
   when `specs_look_deferred()` fires: the page has prose but almost no
   label/value rows while its own markup advertises a spec panel. That panel is
   hydrated client-side, which is the most common way a "successful" scrape
   returns a product page with no product data.
3. **PDFs** — spec sheets and manuals, one evidence unit per page so a citation
   can point at `spec-sheet.pdf#page=3`.

Everything is disk-cached, so re-running a catalogue costs no network at all.

## Reference data

The guide describes seven reference workbooks; only the two item files shipped.
Every store is therefore **pluggable** — drop the real workbook into
`data/reference/` and it takes over automatically. Until then each store
bootstraps (from the delivery format, or from published UOM standards) and
`/v1/reference` reports honestly which mode is active.

| Store | With the workbook | Without it |
| --- | --- | --- |
| `taxonomy.py` | every Classpath in the file | Classpaths in the delivery format + manufacturer breadcrumbs, proposed and flagged |
| `lov.py` | attribute set, sequence and permitted values per leaf | schema inferred from the labelled rows; unknown values emitted and tagged `is_new_lov_value` |
| `uom.py` | the client's approved abbreviations | 87 approved abbreviations / 290 accepted spellings from published standards |
| `brands.py` | manufacturer ↔ brand pairs | read off the manufacturer's own site |

Decimal↔fraction (`textnorm.py`) is computed exactly over 64ths, so no lookup
table is needed: `50.25 → 50-1/4`, `0.5 → 1/2`.

## Cost control

Deterministic parsing does the bulk of the work at zero marginal cost —
JSON-LD, spec tables, definition lists, feature bullets, breadcrumbs, galleries
and document links are all extracted without a model. The LLM is capped at
`llm_max_calls_per_item` (default 6) and typically spends **1–3 calls per item**;
every call is disk-cached on its exact messages.

## Layout

```
app/
  config.py        infrastructure settings, sourcing policy
  textnorm.py      normalisation, fractions, unit splitting  <- defines grounding tolerance
  evidence.py      evidence store + the grounding gate
  describe.py      the five descriptions, constructed
  delivery.py      the 252-column row (columns read from the client's file)
  pipeline.py      orchestration
  main.py          FastAPI
  acquire/         discovery.py  fetcher.py  pdfs.py
  extract/         parse.py  llm.py  select.py
  reference/       taxonomy.py  lov.py  uom.py  brands.py  base.py
scripts/evaluate.py
```

## Honest limitations

* The taxonomy bootstrap has 2 leaves, not 14,000 — accuracy on unseen
  categories will rise sharply once `Unicat_Lov_v1_0_Updated_With_Remarks.xlsx`
  is dropped in. Nothing else needs to change.
* Some manufacturers render their spec panel from a client-side service call
  that the accordion-clicking browser tier cannot always reach. Those rows come
  back with attributes blank and `needs_human_review = true` rather than filled
  with guesses.
* `MANUFACTURER_NAME` and `BRAND_NAME` are left **blank** when they cannot be
  confirmed on a retrieved page. Back-filling them from the input would mean
  writing the distributor's account name into a manufacturer column.
