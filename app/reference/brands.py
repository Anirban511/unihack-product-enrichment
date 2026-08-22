"""Manufacturer / brand resolution.

Two jobs:
  1. strip the placeholder brand fields the distributor sends
     ("-- Unbranded --", "-- No Unilog Brand --", "-- No DIB Brand --" are *empty*,
     not values - guide s.4 "Placeholders are not data");
  2. resolve a messy supplier string to a canonical manufacturer legal name and
     its paired brand, keeping legal casing, suffixes (Inc / LLC / Ltd) and the
     (R) / (TM) symbol exactly as the manufacturer writes them.

The master workbook is pluggable. Without it, the canonical name and brand are
taken from the manufacturer's *own site* (JSON-LD publisher/brand, copyright
line, <title>) - which is the correct source anyway, and keeps the output
grounded rather than dictionary-guessed.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from rapidfuzz import fuzz, process

from app.reference.base import column_like, find_reference, promote_header, read_sheets
from app.textnorm import blank_if_placeholder, clean, is_placeholder, norm

# "Freud Inc (2435)" / "Jam Industrial Supply LLC (JAMIN)" -> supplier account codes
_ACCOUNT_CODE = re.compile(r"\s*\(([A-Z0-9]{2,8})\)\s*$")
_LEGAL_SUFFIX = re.compile(
    r"\b(inc|inc\.|incorporated|llc|l\.l\.c\.|ltd|ltd\.|limited|corp|corp\.|corporation|"
    r"co|co\.|company|gmbh|ag|sa|s\.a\.|bv|b\.v\.|plc|lp|llp|pty|kg|srl|nv|holdings|group)\b\.?",
    re.I,
)


def strip_account_code(s: str) -> Tuple[str, str]:
    """'Freud Inc (2435)' -> ('Freud Inc', '2435')"""
    s = clean(s)
    m = _ACCOUNT_CODE.search(s)
    if not m:
        return s, ""
    return _ACCOUNT_CODE.sub("", s).strip(), m.group(1)


def core_name(s: str) -> str:
    """Suffix-free key for matching. 'Freud Inc' and 'Freud' collapse together."""
    return norm(_LEGAL_SUFFIX.sub("", clean(s))).strip()


class BrandStore:
    def __init__(self) -> None:
        self.source = "runtime:manufacturer-site"
        self.manufacturers: Dict[str, dict] = {}   # core key -> record
        self._load()

    def _load(self) -> None:
        path = (find_reference("manufacturer", "brand") or find_reference("brand", "master")
                or find_reference("manufacturer", "master"))
        if not path:
            return
        try:
            for _n, raw in read_sheets(path).items():
                df = promote_header(raw, ["manufacturer_name", "brand_name", "manufacturer_code"])
                if df is None:
                    continue
                c_mfr = column_like(df, "manufacturer_name", "manufacturer name", "manufacturer")
                c_mcode = column_like(df, "manufacturer_code", "manufacturer code")
                c_brand = column_like(df, "brand_name", "brand name", "brand")
                c_bcode = column_like(df, "brand_code", "brand code")
                if not c_mfr:
                    continue
                for _i, row in df.iterrows():
                    name = clean(row.get(c_mfr, ""))
                    if not name:
                        continue
                    rec = self.manufacturers.setdefault(core_name(name), {
                        "manufacturer_name": name, "manufacturer_code": "",
                        "brands": [], "brand_codes": [],
                    })
                    if c_mcode and clean(row.get(c_mcode, "")):
                        rec["manufacturer_code"] = clean(row.get(c_mcode, ""))
                    if c_brand and clean(row.get(c_brand, "")):
                        b = clean(row.get(c_brand, ""))
                        if b not in rec["brands"]:
                            rec["brands"].append(b)
                    if c_bcode and clean(row.get(c_bcode, "")):
                        rec["brand_codes"].append(clean(row.get(c_bcode, "")))
            if self.manufacturers:
                self.source = "workbook:" + path.name
        except Exception as exc:
            self.source = "runtime (workbook {} unreadable: {})".format(path.name, exc)

    def lookup(self, name: str, cutoff: int = 90) -> Optional[dict]:
        if not self.manufacturers or is_placeholder(name):
            return None
        key = core_name(name)
        if key in self.manufacturers:
            return self.manufacturers[key]
        hit = process.extractOne(key, list(self.manufacturers.keys()),
                                 scorer=fuzz.WRatio, score_cutoff=cutoff)
        return self.manufacturers[hit[0]] if hit else None

    @staticmethod
    def clean_input_brands(row: dict) -> Dict[str, str]:
        """Distributor brand columns pass through untouched except placeholder->''."""
        return {
            "E1_Brand": clean(row.get("E1_Brand", "")),
            "Unilog_Brand": clean(row.get("Unilog_Brand", "")),
            "DIB_Brand": clean(row.get("DIB_Brand", "")),
            "usable_brand": blank_if_placeholder(
                row.get("Unilog_Brand") or row.get("DIB_Brand") or row.get("E1_Brand") or ""
            ),
        }

    def status(self) -> dict:
        return {"source": self.source, "manufacturers": len(self.manufacturers)}


BRANDS = BrandStore()
