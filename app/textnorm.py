"""Normalisation primitives shared by the grounding verifier and every writer.

The single most important function here is `norm()`. Grounding is decided by
normalised containment, so `norm()` defines exactly how forgiving the
zero-hallucination gate is. It folds away things that are *presentation*
(unicode dashes, NBSP, casing, spacing around units) but never things that are
*meaning* (digits, letters, order).
"""
from __future__ import annotations

import re
import unicodedata
from fractions import Fraction
from typing import Iterable, Optional, Tuple

# Presentation-only unicode that must not defeat a verbatim match.
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = {
    ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201a"): "'", ord("\u2032"): "'",
    ord("\u201c"): '"', ord("\u201d"): '"', ord("\u201e"): '"', ord("\u2033"): '"',
}
_SPACES = dict.fromkeys(map(ord, "\u00a0\u2007\u202f\u2009\u200a\u2002\u2003"), " ")
_TRANSLATE = {**_DASHES, **_QUOTES, **_SPACES}

# Vulgar fractions that manufacturers love to paste into spec tables.
_VULGAR = {
    "\u00bc": "1/4", "\u00bd": "1/2", "\u00be": "3/4", "\u2153": "1/3", "\u2154": "2/3",
    "\u215b": "1/8", "\u215c": "3/8", "\u215d": "5/8", "\u215e": "7/8",
    "\u2155": "1/5", "\u2156": "2/5", "\u2157": "3/5", "\u2158": "4/5",
    "\u2159": "1/6", "\u215a": "5/6", "\u2150": "1/7", "\u2151": "1/9", "\u2152": "1/10",
}

TRADEMARKS = "\u00ae\u2122\u2120\u00a9"

PLACEHOLDERS = {
    "-- unbranded --", "-- no unilog brand --", "-- no dib brand --",
    "unbranded", "no brand", "n/a", "na", "none", "null", "-", "--", "",
    "not applicable", "not available", "tbd", "unknown",
}


def strip_control(s: str) -> str:
    return "".join(ch for ch in s if ch == "\n" or ch == "\t" or unicodedata.category(ch)[0] != "C")


# NFKC would expand these into "TM"/"(R)"; the house style keeps the glyph.
_TM_GUARD = {"™": "", "℠": "", "®": ""}


def clean(s: Optional[str]) -> str:
    """Light clean-up that preserves the author's characters (used for output)."""
    if s is None:
        return ""
    s = str(s)
    for glyph, guard in _TM_GUARD.items():
        s = s.replace(glyph, guard)
    s = unicodedata.normalize("NFKC", s)
    for glyph, guard in _TM_GUARD.items():
        s = s.replace(guard, glyph)
    s = s.translate(_TRANSLATE)
    for k, v in _VULGAR.items():
        s = s.replace(k, v)
    s = strip_control(s)
    return re.sub(r"[ \t]+", " ", s).strip()


def norm(s: Optional[str]) -> str:
    """Aggressive fold used *only* for comparison / grounding, never for output."""
    s = clean(s).lower()
    s = s.translate(str.maketrans("", "", TRADEMARKS))
    # "24in" == "24 in" ; "24  in" == "24 in"
    s = re.sub(r"(?<=\d)\s+(?=[a-z\"'])", " ", s)
    s = re.sub(r"[^\w./\-\u00b0]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_tight(s: Optional[str]) -> str:
    """Whitespace-free fold - catches "1/2 in" vs "1/2in" vs "1/2  in"."""
    return re.sub(r"\s+", "", norm(s))


def is_placeholder(s: Optional[str]) -> bool:
    return clean(s).strip().lower() in PLACEHOLDERS


def blank_if_placeholder(s: Optional[str]) -> str:
    return "" if is_placeholder(s) else clean(s)


# ---------------------------------------------------------------------------
# Decimal <-> fraction, exactly reproducing Decimal_Fraction.xlsx (1/64..63/64)
# ---------------------------------------------------------------------------
_DENOM = 64


def decimal_to_fraction(value: float, denominator: int = _DENOM) -> Optional[str]:
    """0.5 -> '1/2'; 50.25 -> '50-1/4'. Returns None if not representable exactly."""
    try:
        f = Fraction(value).limit_denominator(denominator)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    if abs(float(f) - value) > 1e-9:
        return None
    whole, rem = divmod(abs(f.numerator), f.denominator)
    sign = "-" if f < 0 else ""
    if rem == 0:
        return f"{sign}{whole}"
    frac = Fraction(rem, f.denominator)
    return f"{sign}{whole}-{frac.numerator}/{frac.denominator}" if whole else f"{sign}{frac.numerator}/{frac.denominator}"


def fraction_to_decimal(text: str) -> Optional[float]:
    """'50-1/4' -> 50.25 ; '1/2' -> 0.5 ; '24' -> 24.0"""
    t = clean(text).replace(" ", "")
    m = re.fullmatch(r"(-?\d+)[-\s](\d+)/(\d+)", t)
    if m:
        w, n, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        sign = -1 if w < 0 else 1
        return sign * (abs(w) + n / d)
    m = re.fullmatch(r"(-?\d+)/(\d+)", t)
    if m:
        return int(m.group(1)) / int(m.group(2))
    try:
        return float(t)
    except ValueError:
        return None


def to_trade_fraction(text: str) -> str:
    """Manufacturers publish decimals; trade buyers search fractions (guide s.2)."""
    t = clean(text)
    if re.fullmatch(r"-?\d+\.\d+", t):
        f = decimal_to_fraction(float(t))
        if f is not None:
            return f
    return t


def numeric_equivalent(a: str, b: str) -> bool:
    """50.25 == 50-1/4 ; 0.5 == 1/2. Used by the grounding verifier."""
    fa, fb = fraction_to_decimal(a), fraction_to_decimal(b)
    return fa is not None and fb is not None and abs(fa - fb) < 1e-6


def token_set(s: str) -> set:
    return set(norm(s).split())


def contains_normalised(haystack_norm: str, needle: str) -> bool:
    n = norm(needle)
    if not n:
        return False
    if n in haystack_norm:
        return True
    return norm_tight(needle) in re.sub(r"\s+", "", haystack_norm)


def truncate_words(s: str, limit: int) -> str:
    """Never mid-word, never a trailing separator - char limits are graded."""
    s = clean(s)
    if len(s) <= limit:
        return s
    cut = s[:limit]
    for sep in (", ", " - ", " "):
        i = cut.rfind(sep)
        if i > limit * 0.55:
            cut = cut[:i]
            break
    return cut.rstrip(" ,;-|/")


def join_unique(parts: Iterable[str], sep: str = ", ") -> str:
    seen, out = set(), []
    for p in parts:
        p = clean(p)
        if not p or is_placeholder(p):
            continue
        k = norm(p)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return sep.join(out)


def split_number_unit(text: str) -> Tuple[str, str]:
    """'50-1/4 in' -> ('50-1/4', 'in'); '47 dBA' -> ('47','dBA'); 'Leg' -> ('Leg','')."""
    t = clean(text)
    # The unit may be a word ("in", "dBA"), a symbol (\u00b0, %) or a prime mark
    # (24" / 6'), which manufacturers use constantly for inches and feet.
    m = re.fullmatch(
        r"\s*(-?\d+(?:[-\s]\d+/\d+)?(?:\.\d+)?|\d+/\d+)\s*"
        r"([A-Za-z\u00b0\"'][A-Za-z0-9\u00b0/%.\-\"' ]{0,18})?\s*", t)
    if not m:
        return t, ""
    value = re.sub(r"\s+", "-", m.group(1).strip()) if "/" in m.group(1) else m.group(1).strip()
    unit = (m.group(2) or "").strip(" .")
    return value, unit
