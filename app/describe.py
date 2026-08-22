"""The five descriptions.

These are *constructed*, not generated. Every token is either a verified
attribute value, a verified brand/part number, or a connective word from the
house style. No LLM writes a description, which is why a description can never
contain a fact the pipeline could not prove.

Construction formulas and limits follow the delivery format's own worked
example (guide s.3):

  INVOICE_DESC  <= 40 chars, UPPER CASE, trade abbreviations, no spaces in units
  MOBILE_DESC   60-80 chars,  Manufacturer + Product Name + Series + Part Number
  SHORT_DESC    Brand(R) + Series + Part Number + Product Name + With... + attrs
  LONG_DESC1    Brand(R) + Product Name + With... + Series + attrs with UOM
                + "Additional Information: ..."
  RETAIL_DESC   Series + Product Name + key attrs (no brand, no part number)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from app.textnorm import clean, join_unique, norm, truncate_words

INVOICE_MAX = 40
MOBILE_MIN, MOBILE_MAX = 60, 80
SHORT_MAX = 250
LONG_MAX = 2000
RETAIL_MAX = 250

# Trade abbreviations for the till receipt. Value-level, house style.
ABBREV: List[Tuple[str, str]] = [
    ("stainless steel", "SST"), ("stainless", "SST"), ("carbon steel", "CS"),
    ("galvanized", "GALV"), ("aluminum", "ALUM"), ("aluminium", "ALUM"),
    ("black stainless", "BLK SST"), ("fingerprint resistant", "FPR"),
    ("built-in", "BLTLN"), ("built in", "BLTLN"), ("free standing", "FRSTD"),
    ("freestanding", "FRSTD"), ("portable", "PORT"), ("countertop", "CNTRTP"),
    ("under counter", "UNDRCTR"), ("undercounter", "UNDRCTR"),
    ("stainless steel interior", "SST INT"),
    ("professional", "PRO"), ("commercial", "COMM"), ("residential", "RES"),
    ("heavy duty", "HD"), ("medium duty", "MD"), ("light duty", "LD"),
    ("adjustable", "ADJ"), ("automatic", "AUTO"), ("electric", "ELEC"),
    ("electronic", "ELECT"), ("hydraulic", "HYD"), ("pneumatic", "PNEU"),
    ("stainless-steel", "SST"), ("polished chrome", "PCHRM"), ("chrome", "CHRM"),
    ("brushed nickel", "BRSH NKL"), ("oil rubbed bronze", "ORB"),
    ("white", "WHT"), ("black", "BLK"), ("silver", "SLVR"), ("bronze", "BRNZ"),
    ("stainless steel finish", "SST"),
    ("right hand", "RH"), ("left hand", "LH"), ("male", "M"), ("female", "F"),
    ("diameter", "DIA"), ("length", "LG"), ("width", "W"), ("height", "H"),
    ("thickness", "THK"), ("nominal", "NOM"), ("maximum", "MAX"), ("minimum", "MIN"),
    ("package", "PKG"), ("assembly", "ASSY"), ("mounting", "MTG"), ("mount", "MT"),
    ("cartridge", "CTG"), ("connection", "CONN"), ("threaded", "THD"),
    ("cycle", "CYC"), ("cycles", "CYC"), ("with", "W/"), ("without", "W/O"),
]


@dataclass
class Attr:
    """One verified attribute ready for writing."""
    label: str
    value: str
    uom: str = ""
    sequence: int = 0

    @property
    def measure(self) -> str:
        if not self.uom:
            return self.value
        return self.value + self.uom if self.uom in {"%", "°"} else self.value + " " + self.uom


@dataclass
class DescriptionInputs:
    manufacturer: str = ""          # legal name, e.g. "Whirlpool Corporation"
    brand: str = ""                 # marketing brand incl. (R), e.g. "Whirlpool(R)"
    part_number: str = ""
    product_name: str = ""          # "Dishwasher"
    series: str = ""                # "Eco Series"
    with_clause: str = ""           # "With CleanBoost(TM)"
    attributes: List[Attr] = field(default_factory=list)
    additional_information: str = ""


# ---------------------------------------------------------------------------
def _attr(inp: DescriptionInputs, *label_fragments: str) -> Optional[Attr]:
    for frag in label_fragments:
        f = norm(frag)
        for a in inp.attributes:
            if f and f in norm(a.label) and a.value:
                return a
    return None


def _ordered(inp: DescriptionInputs) -> List[Attr]:
    """Schema sequence, with Series first and Additional Information last."""
    body = [a for a in inp.attributes
            if a.value and norm(a.label) not in {"series", "model", "additional information"}]
    return sorted(body, key=lambda a: a.sequence)


# Short / retail / invoice lead with what a buyer picks a product by, in this
# order, regardless of where the attribute sits in the schema sequence.
_HEADLINE_ORDER = ("mounting", "number of", "material", "color", "colour", "finish",
                   "style", "type", "grade", "grit")


def _headline(inp: DescriptionInputs, limit: int = 4) -> List[Attr]:
    ranked: List[Tuple[int, int, Attr]] = []
    for a in _ordered(inp):
        n = norm(a.label)
        if n == "size":
            continue
        for rank, frag in enumerate(_HEADLINE_ORDER):
            if frag in n:
                ranked.append((rank, a.sequence, a))
                break
    ranked.sort(key=lambda t: (t[0], t[1]))
    picked = [a for _r, _s, a in ranked][:limit]
    return picked or _ordered(inp)[:3]


def phrase_for(attr: Attr) -> str:
    """Render one attribute the way the long description writes it.

    'Voltage Rating' + 120 V      -> '120 V'          (unit already names it)
    'Number of Wash Cycles' + 5   -> '5 Wash Cycles'
    'Mounting Type' + Leg         -> 'Leg Mounting'
    'Sound Level' + 47 dBA        -> '47 dBA Sound Level'
    'Material' + Stainless Steel  -> 'Stainless Steel'
    """
    label, measure = clean(attr.label), attr.measure
    n = norm(label)
    if not measure:
        return ""
    if n in {"material", "color", "colour", "finish", "series", "size", "model", "type"}:
        return measure
    if n.endswith("rating") and attr.uom:
        return measure
    m = re.match(r"^number of (.+)$", label, re.I)
    if m:
        return "{} {}".format(measure, m.group(1).strip())
    m = re.match(r"^(.+?)\s+type$", label, re.I)
    if m:
        return "{} {}".format(measure, m.group(1).strip())
    return "{} {}".format(measure, label)


def abbreviate(text: str) -> str:
    t = " " + clean(text).lower() + " "
    for long_form, short in ABBREV:
        t = re.sub(r"(?<![a-z]){}(?![a-z])".format(re.escape(long_form)), short.lower(), t)
    return clean(t).upper()


# ---------------------------------------------------------------------------
def build_invoice(inp: DescriptionInputs) -> str:
    """<= 40 chars, UPPER CASE. Units close up: '120V', '50-1/4IN'."""
    headline = _headline(inp, limit=3)
    rest = [a for a in _ordered(inp) if a not in headline]
    parts: List[str] = [abbreviate(inp.product_name)]
    for a in headline + rest:
        if norm(a.label) in {"additional information", "size"}:
            continue
        token = abbreviate(a.value)
        if a.uom:
            token = "{}{}".format(a.value, a.uom).upper()      # 120V, 15A, 41DBA
        if not token:
            continue
        candidate = " ".join(parts + [token])
        if len(candidate) > INVOICE_MAX:
            break
        parts.append(token)
    return truncate_words(" ".join(parts), INVOICE_MAX).upper()


def build_mobile(inp: DescriptionInputs) -> str:
    """60-80 chars: Manufacturer + Product Name + Series + Part Number (+ filler)."""
    # The mobile line drops the (R)/(TM) glyphs - it is a cramped list row.
    plain_brand = re.sub(r"[®™℠]", "", inp.brand).strip()
    lead = join_unique([inp.manufacturer, plain_brand], sep=" ") if plain_brand else inp.manufacturer
    core = [lead, inp.product_name, inp.series, inp.part_number]
    text = join_unique(core)
    if len(text) < MOBILE_MIN:
        for a in _ordered(inp):
            candidate = text + ", " + phrase_for(a)
            if len(candidate) > MOBILE_MAX:
                continue
            text = candidate
            if len(text) >= MOBILE_MIN:
                break
    return truncate_words(text, MOBILE_MAX)


def build_short(inp: DescriptionInputs) -> str:
    """Brand(R) Series PartNumber ProductName With..., attrs."""
    head = join_unique([inp.brand or inp.manufacturer, inp.series,
                        inp.part_number, inp.product_name], sep=" ")
    if inp.with_clause:
        head = "{} {}".format(head, inp.with_clause)
    tail = [t for t in (phrase_for(a) for a in _headline(inp)) if t]
    return truncate_words(join_unique([head] + tail), SHORT_MAX)


def build_long(inp: DescriptionInputs) -> str:
    """Brand(R) ProductName With..., Series, every verified attribute, extras last."""
    head = join_unique([inp.brand or inp.manufacturer, inp.product_name], sep=" ")
    if inp.with_clause:
        head = "{} {}".format(head, inp.with_clause)
    parts = [head]
    if inp.series:
        parts.append(inp.series)
    parts += [p for p in (phrase_for(a) for a in _ordered(inp)) if p]
    text = join_unique(parts)
    if inp.additional_information:
        text = "{}, Additional Information: {}".format(text, clean(inp.additional_information))
    return truncate_words(text, LONG_MAX)


def build_retail(inp: DescriptionInputs) -> str:
    """Series + Product Name + key attrs. No brand, no part number."""
    head = join_unique([inp.series, inp.product_name], sep=" ")
    tail = [t for t in (phrase_for(a) for a in _headline(inp)) if t]
    return truncate_words(join_unique([head] + tail), RETAIL_MAX)


def build_all(inp: DescriptionInputs) -> Dict[str, str]:
    return {
        "INVOICE_DESC": build_invoice(inp),
        "MOBILE_DESC": build_mobile(inp),
        "SHORT_DESC": build_short(inp),
        "LONG_DESC1": build_long(inp),
        "RETAIL_DESC": build_retail(inp),
    }


LIMITS: Dict[str, Tuple[Optional[int], int]] = {
    "INVOICE_DESC": (None, INVOICE_MAX),
    "MOBILE_DESC": (MOBILE_MIN, MOBILE_MAX),
    "SHORT_DESC": (None, SHORT_MAX),
    "LONG_DESC1": (None, LONG_MAX),
    "RETAIL_DESC": (None, RETAIL_MAX),
}


def compliance(descriptions: Dict[str, str]) -> Dict[str, dict]:
    """Char-limit compliance is a graded metric - report it per field."""
    out: Dict[str, dict] = {}
    for name, (lo, hi) in LIMITS.items():
        text = descriptions.get(name, "") or ""
        ok = len(text) <= hi and (lo is None or not text or len(text) >= lo)
        if name == "INVOICE_DESC" and text and text != text.upper():
            ok = False
        out[name] = {"length": len(text), "min": lo, "max": hi, "compliant": bool(ok)}
    return out
