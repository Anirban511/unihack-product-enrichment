"""Reference-file plumbing.

The solution guide describes seven reference workbooks. Only the two item files
shipped with this challenge, so every store here is *pluggable*: drop the real
workbook into data/reference/ and it takes over automatically; until then a
bootstrapped table derived from the delivery format + published standards is
used, and `status()` reports honestly which mode each store is in.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from app.config import settings


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s).lower())


def find_reference(*name_fragments: str) -> Optional[Path]:
    """Locate a reference workbook by fuzzy filename fragments (order-insensitive)."""
    if not settings.reference_dir.exists():
        return None
    wanted = [_slug(f) for f in name_fragments]
    for p in sorted(settings.reference_dir.rglob("*")):
        if p.suffix.lower() not in {".xlsx", ".xls", ".xlsm", ".csv"}:
            continue
        s = _slug(p.stem)
        if all(w in s for w in wanted):
            return p
    return None


def read_sheets(path: Path) -> Dict[str, "pd.DataFrame"]:
    if path.suffix.lower() == ".csv":
        return {"csv": pd.read_csv(path, dtype=str, keep_default_na=False,
                                   encoding="utf-8-sig", header=None)}
    xl = pd.ExcelFile(path)
    return {n: xl.parse(n, dtype=str, header=None, keep_default_na=False) for n in xl.sheet_names}


def promote_header(df: "pd.DataFrame", expect: List[str], max_scan: int = 12):
    """Guide s.4: 'do not assume row 1 is a clean header'. Scan for the real one."""
    want = [_slug(e) for e in expect]
    for i in range(min(max_scan, len(df))):
        row = [_slug(v) for v in df.iloc[i].tolist()]
        hits = sum(1 for w in want if any(w and w in c for c in row))
        if hits >= max(1, len(want) // 2):
            out = df.iloc[i + 1:].copy()
            out.columns = [str(v).strip() for v in df.iloc[i].tolist()]
            keep = [c for c in out.columns if c and c.lower() != "nan"]
            return out.loc[:, keep]
    return None


def column_like(df: "pd.DataFrame", *fragments: str) -> Optional[str]:
    for frag in fragments:
        f = _slug(frag)
        for c in df.columns:
            if f and f in _slug(c):
                return c
    return None
