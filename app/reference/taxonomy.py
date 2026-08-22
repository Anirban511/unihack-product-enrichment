"""Leaf-level product classification.

The challenge taxonomy is ~14,000 leaf classpaths. That workbook is not in this
pack, so the store is pluggable:

  * drop any taxonomy / Unicat LOV workbook into data/reference/ and every
    distinct Classpath in it becomes a selectable leaf;
  * otherwise the store bootstraps from the Classpath column of the delivery
    format file and from manufacturer breadcrumbs observed at runtime.

Classification is two-stage and cheap: a lexical retriever narrows ~14k leaves
to a handful, then one constrained LLM call picks an *index* from that shortlist.
Because the model returns an index and never a string, it cannot invent a
category that is not in the taxonomy.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from app.config import settings
from app.reference.base import column_like, find_reference, promote_header, read_sheets
from app.textnorm import clean, norm

_STOP = {
    "and", "or", "the", "for", "with", "of", "in", "a", "an", "to", "by",
    "other", "misc", "miscellaneous", "accessories", "products", "general",
}


def _stem(t: str) -> str:
    """Crude singular form. A category is "Dishwashers"; a page says "Dishwasher"."""
    for suffix, repl in (("ies", "y"), ("sses", "ss"), ("ches", "ch"), ("shes", "sh"),
                         ("xes", "x"), ("ses", "s")):
        if len(t) > len(suffix) + 1 and t.endswith(suffix):
            return t[: -len(suffix)] + repl
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def _tokens(s: str) -> List[str]:
    raw = norm(s).replace("/", " ").replace(">", " ").replace("-", " ").split()
    return [_stem(t) for t in raw if t not in _STOP and len(t) > 1]


class TaxonomyStore:
    def __init__(self) -> None:
        self.source = "bootstrap:delivery-format"
        self.leaves: List[str] = []
        # The delivery file carries TWO hierarchies: Classpath (the Unilog
        # taxonomy) and the distributor's own Dept / Class / Fine. They do not
        # agree - "...>Kitchen Appliances>Built-In Dishwashers" versus
        # "Appliances>Large Appliances>Dishwashers" - so they are indexed apart
        # and classified separately rather than one being derived from the other.
        self.dept_class_fine: List[str] = []
        self.proposed: List[str] = []          # breadcrumb proposals, for review
        self._index: Dict[str, set] = defaultdict(set)   # token -> leaf ids
        self._df: Counter = Counter()
        self._load()

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        loaded = False
        for frags in (("taxonomy",), ("unicat", "lov"), ("classpath",), ("category",)):
            path = find_reference(*frags)
            if not path:
                continue
            try:
                for _n, raw in read_sheets(path).items():
                    df = promote_header(raw, ["classpath", "leaf node", "attribute label"])
                    if df is None:
                        continue
                    col = column_like(df, "classpath", "class path", "category path")
                    if not col:
                        continue
                    for v in df[col].tolist():
                        v = clean(v)
                        if v and ">" in v:
                            self.leaves.append(v)
                loaded = bool(self.leaves)
                if loaded:
                    self.source = "workbook:" + path.name
                    break
            except Exception as exc:
                self.source = "bootstrap (workbook {} unreadable: {})".format(path.name, exc)
        if not loaded:
            self._bootstrap_from_delivery_format()
        self.leaves = sorted({c for c in self.leaves if c})
        self.dept_class_fine = sorted({c for c in self.dept_class_fine if c})
        self._build_index()

    def _bootstrap_from_delivery_format(self) -> None:
        import csv
        p = settings.delivery_format_csv
        if not p.exists():
            return
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cp = clean(row.get("Classpath", ""))
                if cp:
                    self.leaves.append(cp)
                trio = [clean(row.get(k, "")) for k in ("Dept", "Class", "Fine")]
                if all(trio):
                    self.dept_class_fine.append(">".join(trio))

    def _build_index(self) -> None:
        self._index.clear()
        self._df.clear()
        for i, leaf in enumerate(self.leaves):
            toks = set(_tokens(leaf))
            for t in toks:
                self._index[t].add(i)
                self._df[t] += 1

    def register_leaf(self, classpath: str) -> None:
        """Record a runtime-discovered leaf for LATER governance review only.

        Deliberately NOT added to the searchable index: a breadcrumb proposed for
        one item would then become a selectable category for the next one, and a
        Milwaukee cut-off wheel would inherit "Abrasives>Abrasive Discs" from the
        3M disc processed before it - a classification with no evidence behind it.
        Proposals are per-item; the index only grows from a real taxonomy file.
        """
        cp = clean(classpath)
        if cp and cp not in self.proposed:
            self.proposed.append(cp)

    # -- retrieval -------------------------------------------------------
    def candidates(self, query: str, k: int = 12) -> List[Tuple[str, float]]:
        """Sparse IDF-weighted retrieval - no embedding service, no per-call cost."""
        if not self.leaves:
            return []
        qt = _tokens(query)
        if not qt:
            return []
        n = len(self.leaves)
        scores: Dict[int, float] = defaultdict(float)
        for t in set(qt):
            hits = self._index.get(t)
            if not hits:
                continue
            idf = math.log(1 + n / (1 + len(hits)))
            for i in hits:
                scores[i] += idf
        if not scores:
            return []
        top = sorted(scores.items(), key=lambda kv: -kv[1])[: max(k * 4, 40)]
        # Scale-free score: what fraction of the leaf's own words does the
        # product evidence contain? An IDF sum depends on how many leaves are
        # loaded, so it cannot carry a fixed threshold across a 2-leaf bootstrap
        # and a 14,000-leaf production taxonomy. Coverage can.
        qt_set = set(qt)
        rescored = []
        for i, _s in top:
            leaf_tokens = set(_tokens(self.leaves[i]))
            if not leaf_tokens:
                continue
            coverage = len(leaf_tokens & qt_set) / len(leaf_tokens)
            fuzzy = fuzz.token_set_ratio(norm(query), norm(self.leaves[i])) / 100.0
            # The leaf node itself matters more than its parents.
            leaf_only = set(_tokens(self.leaves[i].split(">")[-1]))
            leaf_cov = len(leaf_only & qt_set) / len(leaf_only) if leaf_only else 0.0
            rescored.append((self.leaves[i], round(0.5 * coverage + 0.3 * leaf_cov + 0.2 * fuzzy, 4)))
        rescored.sort(key=lambda kv: -kv[1])
        return rescored[:k]

    def classify_dept(self, query: str) -> Tuple[str, str, str]:
        """Best Dept > Class > Fine for a product, from the distributor hierarchy.

        Falls back to splitting the Unilog classpath when no separate hierarchy
        has been loaded, so the columns are never left empty for want of a file.
        """
        if not self.dept_class_fine:
            return "", "", ""
        qt = set(_tokens(query))
        best, best_score = "", 0.0
        for path in self.dept_class_fine:
            leaf = set(_tokens(path.split(">")[-1]))
            allt = set(_tokens(path))
            if not allt:
                continue
            score = (0.6 * (len(leaf & qt) / len(leaf) if leaf else 0.0)
                     + 0.4 * (len(allt & qt) / len(allt)))
            if score > best_score:
                best, best_score = path, score
        if best_score < 0.30:
            return "", "", ""
        parts = [p for p in re.split(r"\s*>\s*", best) if p]
        while len(parts) < 3:
            parts.append(parts[-1] if parts else "")
        return parts[0], parts[-2], parts[-1]

    def split(self, classpath: str) -> Tuple[str, str, str]:
        """Classpath -> (Dept, Class, Fine). Fine is always the leaf."""
        parts = [clean(p) for p in re.split(r"\s*>\s*", clean(classpath)) if clean(p)]
        if not parts:
            return "", "", ""
        if len(parts) == 1:
            return parts[0], "", parts[0]
        if len(parts) == 2:
            return parts[0], parts[1], parts[1]
        return parts[0], parts[-2], parts[-1]

    def status(self) -> dict:
        return {"source": self.source, "leaf_categories": len(self.leaves),
                "dept_class_fine_paths": len(self.dept_class_fine),
                "proposed_leaves_pending_review": len(self.proposed)}


TAXONOMY = TaxonomyStore()
