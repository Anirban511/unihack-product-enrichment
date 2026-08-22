"""List of Values: which attributes a leaf category may carry, and the exact
normalised spelling each value must take.

Contract (guide s.2 "Attribute Standardisation"):
  * attributes come from the category's own attribute set, in the category's
    own sequence - an amperage never lands in a voltage slot;
  * a value is emitted in its LOV-normalised form when the LOV knows it;
  * a genuinely new value is still emitted, but tagged `is_new=True` so the
    delivery layer can mark it for LOV governance.

Pluggable, same as the other stores: Unicat_Lov_v1_0_Updated_With_Remarks.xlsx
(Classpath | Leaf Node | Filtering Y/N | Attribute Label | Attribute Values |
Normalized Label | Normalized Values | Guidelines | Remarks) is read if present.
"""
from __future__ import annotations

import csv
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz, process

from app.config import settings
from app.reference.base import column_like, find_reference, promote_header, read_sheets
from app.textnorm import clean, norm


@dataclass
class AttributeSpec:
    label: str                      # normalised label, e.g. "Voltage Rating"
    sequence: int                   # position within the category (fixed order)
    filterable: bool = False
    values: "OrderedDict[str, str]" = field(default_factory=OrderedDict)  # norm -> canonical
    guidelines: str = ""

    def match(self, value: str, cutoff: int = 90) -> Tuple[str, bool]:
        """Return (canonical_value, is_new)."""
        v = clean(value)
        if not v:
            return "", False
        if not self.values:
            return v, True
        key = norm(v)
        if key in self.values:
            return self.values[key], False
        hit = process.extractOne(key, list(self.values.keys()), scorer=fuzz.WRatio, score_cutoff=cutoff)
        if hit:
            return self.values[hit[0]], False
        return v, True


class LovStore:
    def __init__(self) -> None:
        self.source = "bootstrap:delivery-format"
        self.by_classpath: Dict[str, "OrderedDict[str, AttributeSpec]"] = {}
        self.new_values: List[dict] = []          # audit trail of LOV additions
        self._load()

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        path = find_reference("lov") or find_reference("unicat")
        if path:
            try:
                self._load_workbook(path)
                if self.by_classpath:
                    self.source = "workbook:" + path.name
                    return
            except Exception as exc:
                self.source = "bootstrap (workbook {} unreadable: {})".format(path.name, exc)
        self._bootstrap_from_delivery_format()

    def _load_workbook(self, path) -> None:
        for _n, raw in read_sheets(path).items():
            df = promote_header(raw, ["classpath", "attribute label", "attribute values", "normalized"])
            if df is None:
                continue
            c_cp = column_like(df, "classpath", "class path")
            c_lab = column_like(df, "normalized label", "attribute label", "attribute")
            c_raw_lab = column_like(df, "attribute label")
            c_val = column_like(df, "normalized values", "normalized value")
            c_raw_val = column_like(df, "attribute values", "attribute value")
            c_filt = column_like(df, "filtering", "filterable")
            c_guide = column_like(df, "guideline", "remark")
            if not (c_cp and c_lab):
                continue
            seq: Dict[str, int] = defaultdict(int)
            for _i, row in df.iterrows():
                cp, label = clean(row.get(c_cp, "")), clean(row.get(c_lab, ""))
                if not cp or not label:
                    continue
                bucket = self.by_classpath.setdefault(cp, OrderedDict())
                if label not in bucket:
                    seq[cp] += 1
                    bucket[label] = AttributeSpec(
                        label=label,
                        sequence=seq[cp],
                        filterable=clean(row.get(c_filt, "")).upper().startswith("Y") if c_filt else False,
                        guidelines=clean(row.get(c_guide, "")) if c_guide else "",
                    )
                spec = bucket[label]
                canonical = clean(row.get(c_val, "")) if c_val else ""
                variant = clean(row.get(c_raw_val, "")) if c_raw_val else ""
                for token in (canonical, variant):
                    for piece in re.split(r"\s*[|;]\s*", token):
                        piece = clean(piece)
                        if piece:
                            spec.values.setdefault(norm(piece), canonical or piece)

    def _bootstrap_from_delivery_format(self) -> None:
        """Derive per-classpath attribute schemas from the labelled delivery rows.

        This is the ground-truth file: whatever ATTRIBUTE_LABEL n it uses for a
        classpath IS that classpath's attribute set and sequence.
        """
        p = settings.delivery_format_csv
        if not p.exists():
            return
        with p.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cp = clean(row.get("Classpath", ""))
                if not cp:
                    continue
                bucket = self.by_classpath.setdefault(cp, OrderedDict())
                for i in range(1, 51):
                    label = clean(row.get("ATTRIBUTE_LABEL {}".format(i), ""))
                    if not label:
                        continue
                    if label not in bucket:
                        bucket[label] = AttributeSpec(label=label, sequence=len(bucket) + 1)
                    val = clean(row.get("ATTRIBUTE_VALUE {}".format(i), ""))
                    if val:
                        bucket[label].values.setdefault(norm(val), val)

    # -- api -------------------------------------------------------------
    def has(self, classpath: str) -> bool:
        return clean(classpath) in self.by_classpath

    def schema(self, classpath: str) -> "OrderedDict[str, AttributeSpec]":
        return self.by_classpath.get(clean(classpath), OrderedDict())

    def labels(self, classpath: str) -> List[str]:
        return list(self.schema(classpath).keys())

    def resolve_label(self, classpath: str, raw_label: str, cutoff: int = 88) -> Optional[str]:
        """Map a scraped spec label onto the category's own attribute label."""
        schema = self.schema(classpath)
        if not schema:
            return None
        key = norm(raw_label)
        for lab in schema:
            if norm(lab) == key:
                return lab
        hit = process.extractOne(key, [norm(l) for l in schema], scorer=fuzz.WRatio, score_cutoff=cutoff)
        if not hit:
            return None
        return list(schema.keys())[hit[2]]

    def normalise_value(self, classpath: str, label: str, value: str) -> Tuple[str, bool]:
        spec = self.schema(classpath).get(clean(label))
        if spec is None:
            return clean(value), True
        canonical, is_new = spec.match(value)
        if is_new and canonical:
            spec.values.setdefault(norm(canonical), canonical)   # LOV grows, as instructed
            self.new_values.append({"classpath": classpath, "attribute": label, "value": canonical})
        return canonical, is_new

    def status(self) -> dict:
        return {
            "source": self.source,
            "classpaths_with_schema": len(self.by_classpath),
            "attribute_specs": sum(len(v) for v in self.by_classpath.values()),
            "new_values_recorded": len(self.new_values),
        }


LOV = LovStore()
