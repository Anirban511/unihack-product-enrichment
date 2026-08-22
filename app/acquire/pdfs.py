"""Spec sheets and manuals are where the real numbers live.

A manufacturer product page often shows marketing copy and hides the actual
electrical / dimensional data in a linked PDF. Those PDFs are downloaded, text
is extracted per page, and each page becomes its own evidence unit so a citation
can point at `...spec-sheet.pdf#page=3` rather than at the whole document.
"""
from __future__ import annotations

import io
import re
from typing import Dict, List, Optional, Tuple

from app.acquire.fetcher import fetch
from app.config import settings
from app.evidence import EvidenceStore
from app.textnorm import clean

_LABELLED_LINE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 ()/,.'\"\-&+%]{2,48}?)\s*[:•]\s*(.{1,160}?)\s*$")
_DOTTED_LINE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 ()/,.'\"\-&+%]{2,48}?)\s*[.·]{3,}\s*(.{1,80}?)\s*$")
_COLUMNAR = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 ()/,.'\"\-&+%]{2,48}?)\s{3,}(.{1,80}?)\s*$")


def extract_pdf_pages(data: bytes, max_pages: int = 40) -> List[str]:
    """Text per page. pypdf first (fast, pure-python); pymupdf if it is installed."""
    pages: List[str] = []
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data))
        for page in reader.pages[:max_pages]:
            pages.append(page.extract_text() or "")
    except Exception:
        pages = []
    if any(len(p.strip()) > 40 for p in pages):
        return pages
    try:  # vector-drawn or scanned PDFs defeat pypdf
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        return [doc[i].get_text() for i in range(min(len(doc), max_pages))]
    except Exception:
        return pages


def harvest_pdf(url: str, store: EvidenceStore, tier: int = 1,
                doc_type: str = "") -> Tuple[int, List[Tuple[str, str, str]]]:
    """Add one evidence unit per page + one per detected label/value line.

    Returns (pages_ingested, [(label, value, evidence_id), ...]).
    """
    res = fetch(url, allow_browser=False)
    if not res.ok or not res.body:
        return 0, []
    pages = extract_pdf_pages(res.body)
    specs: List[Tuple[str, str, str]] = []
    ingested = 0
    for i, text in enumerate(pages, start=1):
        text = clean(re.sub(r"[ \t]{2,}", "   ", text or ""))
        if len(text) < 40:
            continue
        ingested += 1
        store.add(res.final_url or url, "pdf", text[:6000], tier=tier, page=i,
                  doc_title=doc_type or "PDF")
        for raw_line in (pages[i - 1] or "").splitlines():
            line = clean(raw_line)
            if not (4 < len(line) < 200):
                continue
            m = _LABELLED_LINE.match(line) or _DOTTED_LINE.match(line) or _COLUMNAR.match(line)
            if not m:
                continue
            label, value = clean(m.group(1)), clean(m.group(2))
            if not label or not value or label.lower() == value.lower():
                continue
            if len(value) > 120 or not re.search(r"[A-Za-z0-9]", value):
                continue
            ev = store.add(res.final_url or url, "spec_pair",
                           "{}: {}".format(label, value), tier=tier, label=label,
                           value=value, page=i, doc_title=doc_type or "PDF")
            if ev:
                specs.append((label, value, ev.id))
    return ingested, specs


def harvest_documents(documents: Dict[str, List[str]], store: EvidenceStore,
                      tier: int = 1, limit: Optional[int] = None) -> Dict[str, int]:
    """Ingest the most information-dense document types first, within budget."""
    limit = limit or settings.max_pdfs_per_item
    priority = ["Specification Sheet", "Submittal", "Technical Bulletin", "Line Drawing",
                "Owners/User Manual", "Instruction/Installation Manual", "Service Manual",
                "Energy Star Guide", "Catalog", "Warranty Information"]
    ordered: List[Tuple[str, str]] = []
    for doc_type in priority:
        for u in documents.get(doc_type, [])[:2]:
            ordered.append((doc_type, u))
    for doc_type, urls in documents.items():
        if doc_type not in priority:
            for u in urls[:1]:
                ordered.append((doc_type, u))

    out: Dict[str, int] = {}
    for doc_type, u in ordered[:limit]:
        try:
            pages, _specs = harvest_pdf(u, store, tier=tier, doc_type=doc_type)
            if pages:
                out[doc_type] = out.get(doc_type, 0) + pages
        except Exception:
            continue
    return out
