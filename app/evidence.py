"""The evidence store and the grounding gate.

This is the module that makes "no LLM hallucinations" a mechanical property of
the system rather than a promise in a prompt.

Every candidate value that wants to reach the delivery file must first be
*re-found* in the raw source text that was actually downloaded. The check is a
normalised containment test, plus a numeric-equivalence test so that
`50-1/4 in` still verifies against a page that printed `50.25"`. Anything that
cannot be re-found is dropped and recorded in `rejections` - so the pipeline can
report precisely what it refused to say, which is the interesting half of an
accuracy story.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from rapidfuzz import fuzz

from app.config import settings
from app.textnorm import clean, norm, norm_tight, numeric_equivalent


@dataclass
class Evidence:
    id: str
    url: str
    kind: str                 # jsonld | spec_pair | feature | marketing | meta | pdf | breadcrumb | asset
    text: str
    tier: int = 1             # 1 manufacturer, 2 distributor
    label: str = ""           # spec pairs only
    value: str = ""
    page: Optional[int] = None
    doc_title: str = ""

    @property
    def citation(self) -> str:
        """A link a human can click to see the value with their own eyes."""
        if self.page:
            return "{}#page={}".format(self.url, self.page)
        return self.url


@dataclass
class Grounding:
    ok: bool
    evidence_id: str = ""
    citation: str = ""
    ratio: float = 0.0
    method: str = ""
    tier: int = 0


@dataclass
class Rejection:
    field: str
    value: str
    reason: str


class EvidenceStore:
    def __init__(self) -> None:
        self.items: Dict[str, Evidence] = {}
        self._norm_cache: Dict[str, str] = {}
        self._tight_cache: Dict[str, str] = {}
        self.rejections: List[Rejection] = []
        self._n = 0

    # -- population ------------------------------------------------------
    def add(self, url: str, kind: str, text: str, tier: int = 1, label: str = "",
            value: str = "", page: Optional[int] = None, doc_title: str = "") -> Optional[Evidence]:
        text = clean(text)
        if not text:
            return None
        self._n += 1
        ev = Evidence(id="E{}".format(self._n), url=url, kind=kind, text=text, tier=tier,
                      label=clean(label), value=clean(value), page=page, doc_title=clean(doc_title))
        self.items[ev.id] = ev
        self._norm_cache[ev.id] = norm(text)
        self._tight_cache[ev.id] = norm_tight(text)
        return ev

    def get(self, ev_id: str) -> Optional[Evidence]:
        return self.items.get(clean(ev_id))

    def by_kind(self, *kinds: str) -> List[Evidence]:
        ks = set(kinds)
        return [e for e in self.items.values() if e.kind in ks]

    @property
    def tier(self) -> int:
        return min([e.tier for e in self.items.values()], default=0)

    # -- the gate --------------------------------------------------------
    def verify(self, value: str, prefer: Optional[Iterable[str]] = None,
               field_name: str = "") -> Grounding:
        """Re-find `value` in the downloaded sources. Manufacturer tier wins ties."""
        v = clean(value)
        if not v:
            return Grounding(ok=False, method="empty")

        order: List[Evidence] = []
        if prefer:
            order += [self.items[i] for i in (clean(p) for p in prefer) if i in self.items]
        order += sorted(self.items.values(), key=lambda e: (e.tier, e.id))

        best = Grounding(ok=False)
        nv, tv = norm(v), norm_tight(v)
        seen = set()
        for ev in order:
            if ev.id in seen:
                continue
            seen.add(ev.id)
            hay, tight = self._norm_cache[ev.id], self._tight_cache[ev.id]

            if nv and nv in hay:
                return Grounding(True, ev.id, ev.citation, 1.0, "exact", ev.tier)
            if tv and tv in tight:
                return Grounding(True, ev.id, ev.citation, 1.0, "exact-tight", ev.tier)

            # 50-1/4 in  <->  50.25"   (guide s.2 Decimal_Fraction)
            if ev.value and numeric_equivalent(v, ev.value):
                return Grounding(True, ev.id, ev.citation, 1.0, "numeric-equivalent", ev.tier)
            m = re.fullmatch(r"(-?[\d./\-]+)\s*([A-Za-z°%/]*)", v)
            if m:
                num, unit = m.group(1), norm(m.group(2))
                for cand in re.findall(r"-?\d+(?:\.\d+)?(?:[-\s]\d+/\d+)?|\d+/\d+", hay):
                    if numeric_equivalent(num, cand) and (not unit or unit in hay):
                        return Grounding(True, ev.id, ev.citation, 0.98, "numeric-scan", ev.tier)

            # Long verbatim prose (marketing copy) can differ by entity decoding.
            if len(nv) >= 40:
                r = fuzz.partial_ratio(nv, hay) / 100.0
                if r > best.ratio:
                    best = Grounding(r >= settings.grounding_min_ratio, ev.id, ev.citation,
                                     r, "partial", ev.tier)

        if not best.ok and field_name:
            self.rejections.append(Rejection(field=field_name, value=v,
                                             reason="not found in any retrieved source"))
        return best

    def gate(self, field_name: str, value: str,
             prefer: Optional[Iterable[str]] = None) -> Tuple[str, Grounding]:
        """Return ('' , failed-grounding) when a value cannot be proven."""
        g = self.verify(value, prefer=prefer, field_name=field_name)
        return (clean(value) if g.ok else ""), g

    # -- prompt material -------------------------------------------------
    def prompt_block(self, kinds: Iterable[str] = (), max_chars: int = 26000) -> str:
        """Render evidence for the LLM with ids attached, so it can only cite."""
        ks = set(kinds)
        chosen = [e for e in self.items.values() if not ks or e.kind in ks]
        chosen.sort(key=lambda e: (e.tier, {"spec_pair": 0, "jsonld": 1, "feature": 2,
                                            "meta": 3, "pdf": 4, "marketing": 5}.get(e.kind, 6)))
        out, used = [], 0
        for e in chosen:
            body = e.text if len(e.text) <= 900 else e.text[:900] + " ..."
            line = "[{}] ({}) {}".format(e.id, e.kind, body)
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line)
        return "\n".join(out)

    def stats(self) -> dict:
        by_kind: Dict[str, int] = {}
        for e in self.items.values():
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        return {"evidence_units": len(self.items), "by_kind": by_kind,
                "rejected_values": len(self.rejections)}
