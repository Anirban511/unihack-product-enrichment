"""The 252-column delivery row.

The column list is never hardcoded - it is read from the delivery-format file
itself, so if the client ships a new version of that file the output follows it
without a code change. Anything the pipeline could not prove stays blank, which
is the correct answer for a column with no evidence behind it.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Dict, List, Optional

from app.config import settings
from app.textnorm import clean

_COLUMNS: Optional[List[str]] = None

MAX_REF_URLS = 5
MAX_FEATURES = 20
MAX_ATTRIBUTES = 50
MAX_ALT_IMAGES = 4


def columns() -> List[str]:
    global _COLUMNS
    if _COLUMNS is None:
        p = settings.delivery_format_csv
        if p.exists():
            with p.open(encoding="utf-8-sig", newline="") as fh:
                _COLUMNS = [c.strip() for c in next(csv.reader(fh))]
        else:  # keep the API alive even without the reference file
            _COLUMNS = ["MFR URL", "PART_NUMBER", "Mfg_Part_Num"]
    return list(_COLUMNS)


def blank_row() -> Dict[str, str]:
    return {c: "" for c in columns()}


def asset_filename(brand_or_mfr: str, part_number: str, url: str,
                   suffix: str = "", default_ext: str = ".jpg") -> str:
    """'Whirlpool_WDTS7024RZ_Specification_Sheet.pdf' - the delivery naming style."""
    def tok(s: str) -> str:
        return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]+", "_", clean(s))).strip("_")

    # A DAM URL often names a source format but renders another: the query
    # string wins ("hero.tif?fmt=jpg" is delivered as a JPEG).
    ext = ""
    fmt = re.search(r"[?&](?:fmt|format)=([A-Za-z0-9]{2,5})", url or "")
    if fmt:
        ext = "." + fmt.group(1).lower()
    else:
        m = re.search(r"\.([A-Za-z0-9]{2,5})(?:$|\?)", (url or "").split("#")[0])
        if m:
            ext = "." + m.group(1).lower()
    if ext in (".aspx", ".php", ".html", ".htm", ".ashx", ""):
        ext = ""
    if ext in (".tif", ".tiff"):          # print masters are delivered as JPEG
        ext = ".jpg"
    ext = ext or default_ext
    parts = [tok(brand_or_mfr), tok(part_number)]
    if suffix:
        parts.append(tok(suffix))
    return "_".join(p for p in parts if p) + ext


def set_series(row: Dict[str, str], template: str, values: List[str], limit: int) -> None:
    """Fill ITEM_FEATURES_1..n / Ref URL 1..n style column families."""
    cols = set(row.keys())
    for i, v in enumerate(values[:limit], start=1):
        col = template.format(i)
        if col in cols:
            row[col] = clean(v)


def set_attributes(row: Dict[str, str], attributes: List[dict]) -> None:
    """ATTRIBUTE_LABEL n / ATTRIBUTE_VALUE n / ATTRIBUTE_UOM n, in schema sequence.

    The label is written even when the value is empty: the delivery format keeps
    the category's full attribute set so a blank is visibly a blank, not a
    missing column.
    """
    cols = set(row.keys())
    for i, a in enumerate(attributes[:MAX_ATTRIBUTES], start=1):
        for suffix, key in (("LABEL", "label"), ("VALUE", "value"), ("UOM", "uom")):
            col = "ATTRIBUTE_{} {}".format(suffix, i)
            if col in cols:
                row[col] = clean(a.get(key, ""))


def to_csv(rows: List[Dict[str, str]]) -> str:
    cols = columns()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    return buf.getvalue()
