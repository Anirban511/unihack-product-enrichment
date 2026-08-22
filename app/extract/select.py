"""Constrained selection tasks.

Every function here hands the model a numbered list and asks for numbers back.
The returned index is used to look the value up in the caller's own array, so
the text that reaches the delivery file is always text that was scraped, byte
for byte. If the model returns an out-of-range index, a duplicate, or nothing at
all, the deterministic fallback runs instead and the item carries on.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from app.extract.llm import LlmBudget, json_call
from app.textnorm import clean, norm

_SYS = (
    "You are a product-data classification engine for an industrial distributor. "
    "You never write product facts. You only choose from the numbered options you "
    "are given, and you answer with JSON containing option numbers. "
    "If no option is correct, return the null/empty answer rather than inventing one."
)


def _ints(value, lo: int, hi: int) -> List[int]:
    out: List[int] = []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        return out
    for v in value:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if lo <= i <= hi and i not in out:
            out.append(i)
    return out


def _numbered(items: Sequence[str], limit: int = 120, width: int = 180) -> str:
    return "\n".join("{}. {}".format(i, clean(t)[:width]) for i, t in enumerate(items[:limit]))


# ---------------------------------------------------------------------------
def choose_classpath(context: str, candidates: List[str], budget: LlmBudget
                     ) -> Tuple[Optional[str], float, str]:
    """Pick the leaf category. Returns (classpath, confidence, method)."""
    if not candidates:
        return None, 0.0, "no-candidates"
    if len(candidates) == 1:
        return candidates[0], 0.6, "single-candidate"

    data = json_call(
        _SYS,
        "Classify this product into exactly one leaf category.\n\n"
        "PRODUCT EVIDENCE:\n{}\n\nCANDIDATE LEAF CATEGORIES:\n{}\n\n"
        "Answer JSON: {{\"index\": <number or null>, \"confidence\": <0.0-1.0>}}"
        .format(clean(context)[:2500], _numbered(candidates, limit=40, width=140)),
        budget, max_tokens=2500,
    )
    if data:
        idx = _ints(data.get("index"), 0, len(candidates) - 1)
        if idx:
            try:
                conf = float(data.get("confidence", 0.7))
            except (TypeError, ValueError):
                conf = 0.7
            return candidates[idx[0]], max(0.0, min(1.0, conf)), "llm-rerank"
    return candidates[0], 0.45, "lexical-fallback"


# ---------------------------------------------------------------------------
def map_spec_labels(classpath: str, target_labels: List[str],
                    pairs: List[Tuple[str, str, str]], budget: LlmBudget,
                    already: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """Map category attributes onto scraped label/value pairs.

    Returns {attribute_label: index into `pairs`}. The *value* is never taken
    from the model - only the index of the pair it pointed at.
    """
    already = dict(already or {})
    open_labels = [l for l in target_labels if l not in already]
    if not open_labels or not pairs:
        return already

    listing = ["{} = {}".format(clean(l)[:60], clean(v)[:90]) for l, v, _ in pairs]
    data = json_call(
        _SYS,
        "Category: {}\n\n"
        "The distributor's schema requires these attributes:\n{}\n\n"
        "These label/value pairs were scraped from the manufacturer's own page "
        "and documents:\n{}\n\n"
        "For each schema attribute, give the number of the ONE scraped pair that "
        "states it. An amperage may only map to an amperage attribute, a voltage "
        "only to a voltage attribute. Omit any attribute that no pair states - do "
        "not guess, and never map the same pair to two attributes.\n"
        "Answer JSON: {{\"mappings\": [{{\"attribute\": \"<exact schema attribute>\", "
        "\"pair\": <number>}}]}}"
        .format(clean(classpath), _numbered(open_labels, limit=60, width=60),
                _numbered(listing, limit=110, width=150)),
        budget, max_tokens=6000,
    )
    if not data:
        return already

    used = set(already.values())
    valid = {norm(l): l for l in open_labels}
    for m in (data.get("mappings") or []):
        if not isinstance(m, dict):
            continue
        label = valid.get(norm(str(m.get("attribute", ""))))
        idx = _ints(m.get("pair"), 0, len(pairs) - 1)
        if not label or not idx or idx[0] in used or label in already:
            continue
        already[label] = idx[0]
        used.add(idx[0])
    return already


# ---------------------------------------------------------------------------
def choose_product_name(candidates: List[str], context: str, budget: LlmBudget
                        ) -> Tuple[str, str]:
    """The noun the buyer searches for: 'Dishwasher', 'Sanding Belt'.

    Candidates are generated from the breadcrumb leaf, the category name and the
    page title, so the answer is always a phrase the manufacturer used.
    """
    candidates = [c for c in dict.fromkeys(clean(c) for c in candidates) if c]
    if not candidates:
        return "", "no-candidates"
    if len(candidates) == 1:
        return candidates[0], "single-candidate"

    data = json_call(
        _SYS,
        "Pick the short generic product-type noun for this item - the words a "
        "trade buyer would type, singular, with no brand, model number, colour or "
        "size in it.\n\nPRODUCT EVIDENCE:\n{}\n\nOPTIONS:\n{}\n\n"
        "Answer JSON: {{\"index\": <number>}}"
        .format(clean(context)[:1200], _numbered(candidates, limit=25, width=80)),
        budget, max_tokens=2000,
    )
    if data:
        idx = _ints(data.get("index"), 0, len(candidates) - 1)
        if idx:
            return candidates[idx[0]], "llm-choice"
    return candidates[0], "first-candidate"


# ---------------------------------------------------------------------------
def rank_features(features: List[str], context: str, budget: LlmBudget,
                  keep: int = 20) -> List[int]:
    """Order scraped bullets by how much a buyer cares. Text is never rewritten."""
    if len(features) <= keep:
        return list(range(len(features)))

    data = json_call(
        _SYS,
        "These bullets were scraped from one manufacturer product page. Some are "
        "genuine product features, some are site furniture or unrelated items.\n\n"
        "PRODUCT: {}\n\nBULLETS:\n{}\n\n"
        "Return the numbers of up to {} genuine features for THIS product, most "
        "important first. Exclude anything that is navigation, legal text, "
        "reviews, pricing or a different product.\n"
        "Answer JSON: {{\"keep\": [<numbers>]}}"
        .format(clean(context)[:600], _numbered(features, limit=90, width=110), keep),
        budget, max_tokens=3000,
    )
    if data:
        idx = _ints(data.get("keep"), 0, len(features) - 1)
        if idx:
            return idx[:keep]
    return list(range(min(keep, len(features))))


# ---------------------------------------------------------------------------
def choose_primary_image(images: List[str], mpn: str, budget: LlmBudget) -> Optional[int]:
    """Deterministic first (MPN in the filename); the model only breaks ties."""
    if not images:
        return None
    m = norm(mpn).replace(" ", "")
    scored = []
    for i, u in enumerate(images):
        low = u.lower()
        s = 0.0
        if m and m in re.sub(r"[^a-z0-9]", "", low):
            s += 5.0
        for kw, w in (("hero", 2.0), ("main", 1.5), ("primary", 1.5), ("front", 1.0),
                      ("product", 0.8), ("_1.", 0.4), ("angle", 0.3)):
            if kw in low:
                s += w
        scored.append((s, i))
    scored.sort(key=lambda t: (-t[0], t[1]))
    if scored and scored[0][0] >= 2.0:
        return scored[0][1]

    data = json_call(
        _SYS,
        "Which URL is most likely the main product photograph for part {}? "
        "Exclude logos, banners, icons and lifestyle imagery.\n\n{}\n\n"
        "Answer JSON: {{\"index\": <number or null>}}"
        .format(clean(mpn), _numbered(images, limit=25, width=160)),
        budget, max_tokens=1800,
    )
    if data:
        idx = _ints(data.get("index"), 0, len(images) - 1)
        if idx:
            return idx[0]
    return scored[0][1] if scored else None
