"""FastAPI service.

    POST /v1/enrich          one part in, one 252-column delivery row out
    POST /v1/enrich/batch    many parts, bounded concurrency
    POST /v1/enrich/csv      upload the input CSV, download the delivery CSV
    GET  /v1/reference       which reference tables are loaded, and from where
    GET  /v1/health          liveness + browser/LLM/cache readiness
"""
from __future__ import annotations

import asyncio
import csv
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.acquire import fetcher
from app.config import settings
from app.delivery import columns, to_csv
from app.extract import llm
from app.pipeline import EnrichmentInput, EnrichmentResult, enrich
from app.reference.brands import BRANDS
from app.reference.lov import LOV
from app.reference.taxonomy import TAXONOMY
from app.reference.uom import UOM

app = FastAPI(
    title="Unilog Product Enrichment Pipeline",
    version="1.0.0",
    description=(
        "Manufacturer Name + Part Number in; a complete, standardised, "
        "source-cited product record out. Every emitted value is re-verified "
        "against the document it was taken from before it is written."
    ),
)

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="enrich")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ItemRequest(BaseModel):
    Mfg_Part_Num: str = Field(..., description="Manufacturer part number", min_length=1)
    Part_Desc: str = Field("", description="Distributor's short description")
    E1_Brand: str = ""
    Unilog_Brand: str = ""
    DIB_Brand: str = ""
    Part_Manuf: str = Field("", description="Supplier/manufacturer string from the source system")
    SKU: str = ""
    Dept: str = ""
    Class: str = ""
    Fine: str = ""

    @field_validator("Mfg_Part_Num")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Mfg_Part_Num must not be blank")
        return v.strip()

    def to_input(self) -> EnrichmentInput:
        return EnrichmentInput(**self.model_dump())


class BatchRequest(BaseModel):
    items: List[ItemRequest] = Field(..., min_length=1, max_length=200)
    concurrency: int = Field(3, ge=1, le=8)


class ProvenanceOut(BaseModel):
    field: str
    value: str
    source_url: str
    evidence_id: str = ""
    method: str = ""
    confidence: float = 0.0
    tier: int = 0
    is_new_lov_value: bool = False


class EnrichResponse(BaseModel):
    input: dict
    confidence: float
    needs_human_review: bool
    warnings: List[str]
    delivery_row: Dict[str, str]
    provenance: List[ProvenanceOut]
    sources: List[dict]
    metrics: dict


def _serialise(result: EnrichmentResult) -> dict:
    return {
        "input": result.input,
        "confidence": result.confidence,
        "needs_human_review": result.needs_human_review,
        "warnings": result.warnings,
        "delivery_row": result.delivery_row,
        "provenance": [asdict(p) for p in result.provenance],
        "sources": result.sources,
        "metrics": result.metrics,
    }


async def _run(item: EnrichmentInput) -> EnrichmentResult:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_pool, enrich, item)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.post("/v1/enrich", response_model=EnrichResponse, summary="Enrich one part")
async def enrich_one(req: ItemRequest) -> JSONResponse:
    result = await _run(req.to_input())
    return JSONResponse(_serialise(result))


@app.post("/v1/enrich/batch", summary="Enrich many parts")
async def enrich_batch(req: BatchRequest) -> JSONResponse:
    gate = asyncio.Semaphore(req.concurrency)

    async def one(item: ItemRequest) -> dict:
        async with gate:
            try:
                return _serialise(await _run(item.to_input()))
            except Exception as exc:      # one bad row must never fail the batch
                return {"input": item.model_dump(), "error": "{}: {}".format(
                    type(exc).__name__, str(exc)[:300]), "needs_human_review": True,
                    "confidence": 0.0, "delivery_row": {}, "provenance": [],
                    "sources": [], "warnings": ["row failed"], "metrics": {}}

    results = await asyncio.gather(*(one(i) for i in req.items))
    ok = [r for r in results if not r.get("error")]
    return JSONResponse({
        "count": len(results),
        "succeeded": len(ok),
        "failed": len(results) - len(ok),
        "mean_confidence": round(sum(r["confidence"] for r in ok) / len(ok), 3) if ok else 0.0,
        "needs_review": sum(1 for r in results if r.get("needs_human_review")),
        "results": results,
    })


@app.post("/v1/enrich/csv", summary="Input CSV in, delivery-format CSV out")
async def enrich_csv(
    file: UploadFile = File(..., description="CSV with a Mfg_Part_Num column"),
    limit: int = Query(25, ge=1, le=500, description="Rows to process"),
    offset: int = Query(0, ge=0),
    concurrency: int = Query(3, ge=1, le=8),
) -> StreamingResponse:
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    reader = list(csv.DictReader(io.StringIO(raw)))
    if not reader:
        raise HTTPException(400, "CSV contained no data rows")
    if "Mfg_Part_Num" not in (reader[0].keys()):
        raise HTTPException(400, "CSV must contain a Mfg_Part_Num column; got {}".format(
            list(reader[0].keys())[:10]))

    window = reader[offset: offset + limit]
    gate = asyncio.Semaphore(concurrency)

    async def one(row: dict):
        async with gate:
            item = EnrichmentInput(**{k: (row.get(k) or "") for k in
                                      EnrichmentInput.__dataclass_fields__ if k in row})
            item.Mfg_Part_Num = row.get("Mfg_Part_Num", "")
            try:
                return await _run(item)
            except Exception:
                return None

    results = await asyncio.gather(*(one(r) for r in window))
    rows = [r.delivery_row for r in results if r is not None]
    if not rows:
        raise HTTPException(502, "no row could be enriched from an allowed source")

    body = to_csv(rows)
    return StreamingResponse(
        io.BytesIO(body.encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="delivery_format.csv"',
                 "X-Rows-Requested": str(len(window)), "X-Rows-Delivered": str(len(rows))},
    )


@app.get("/v1/reference", summary="Loaded reference tables and their provenance")
async def reference_status() -> dict:
    return {
        "reference_dir": str(settings.reference_dir),
        "note": ("Each store prefers the client's workbook when it is present in "
                 "reference_dir and falls back to a bootstrapped table otherwise. "
                 "'source' tells you which mode is active."),
        "taxonomy": TAXONOMY.status(),
        "lov": LOV.status(),
        "uom": UOM.status(),
        "brands": BRANDS.status(),
        "delivery_columns": len(columns()),
    }


@app.get("/v1/health", summary="Liveness and readiness")
async def health() -> dict:
    return {
        "status": "ok",
        "llm": llm.stats(),
        "http_cache": fetcher.cache_stats(),
        "sourcing_policy": {
            "tier_1": "manufacturer's own domain",
            "tier_2": "reputed industrial distributors (fallback only)",
            "banned": "consumer marketplaces, always",
            "banned_domain_rules": len(settings.banned_domains),
        },
        "grounding": {
            "min_ratio": settings.grounding_min_ratio,
            "review_threshold": settings.review_confidence_threshold,
        },
    }


@app.on_event("shutdown")
def _shutdown() -> None:
    fetcher.shutdown()
