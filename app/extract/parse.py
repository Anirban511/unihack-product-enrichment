"""HTML -> evidence.

Everything a well-behaved manufacturer page already states in machine-readable
form is taken deterministically here: JSON-LD, spec tables, definition lists,
feature bullets, breadcrumbs, gallery images, document links. No LLM is involved,
so this path costs nothing and cannot hallucinate.

The LLM is only asked to do the one thing parsing cannot: decide which of the
scraped labels corresponds to which category attribute.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.evidence import EvidenceStore
from app.textnorm import clean, norm

# --- document-type routing (delivery-format asset columns) -----------------
DOC_PATTERNS: List[Tuple[str, Tuple[str, ...]]] = [
    ("SDS", ("safety data sheet", "sds", "msds", "material safety")),
    ("Warranty Information", ("warranty", "guarantee")),
    ("Specification Sheet", ("spec sheet", "specification sheet", "specsheet", "product spec",
                             "technical specification", "tech spec", "specifications sheet",
                             "product-spec", "cut sheet", "cutsheet", "data sheet", "datasheet")),
    ("Instruction/Installation Manual", ("installation", "install guide", "install manual",
                                         "instruction", "quick start", "use and care",
                                         "getting started", "assembly")),
    ("Service Manual", ("service manual", "repair manual", "tech sheet", "service guide",
                        "parts list", "parts manual", "parts diagram")),
    ("Owners/User Manual", ("owner", "user manual", "user guide", "operator", "manual")),
    ("Energy Star Guide", ("energy guide", "energy star", "energyguide", "eu energy label")),
    ("Line Drawing", ("line drawing", "dimension drawing", "dimensional drawing", "outline drawing")),
    ("Full Engineering Drawing", ("engineering drawing", "cad", "dwg", "step file", "3d model")),
    ("Submittal", ("submittal", "submittal sheet")),
    ("MTR", ("mill test", "material test report", "mtr", "certificate of conformance", "coc")),
    ("RoHS", ("rohs", "reach", "conflict mineral", "prop 65 certificate")),
    ("Technical Bulletin", ("technical bulletin", "service bulletin", "tech bulletin",
                            "application note", "white paper")),
    ("Compatibility Chart", ("compatibility", "cross reference", "cross-reference", "interchange")),
    ("Size Chart", ("size chart", "sizing chart", "size guide", "selection chart")),
    ("Product Label/Insert", ("product label", "insert", "packaging label")),
    ("Catalog", ("catalog", "catalogue", "brochure", "literature", "flyer", "price list")),
]

_SPEC_HINT = re.compile(
    r"(spec|attribute|detail|technical|dimension|feature|property|characteristic|"
    r"parameter|rating|product-info|prod-info|tech-data)", re.I)
_FEATURE_HEAD = re.compile(r"(feature|highlight|benefit|what.s included|includes|"
                           r"specifications? & features)", re.I)
_JUNK_LABEL = re.compile(
    r"^(share|print|compare|add to|wishlist|quantity|qty|price|reviews?|rating|"
    r"availability|in stock|out of stock|find a|where to buy|sign in|sign up|search|"
    r"menu|home|cart|email|phone|address|copyright|cookie|privacy|terms|show|hide|"
    r"yes|no|skip|continue|proceed|select|verify|verification|success|attention|"
    r"pending|check your|didn.t get|current location|provide your|order|total|"
    r"estimated|delivery|shipping|notify|subscribe|newsletter|chat|help|support|"
    r"language|english|fran|outlet|close|cancel|apply|filter|sort)\b", re.I)
# Site chrome that regularly hides inside "spec-ish" containers.
_JUNK_TEXT = re.compile(
    r"(add to cart|postal code|zip code|password|verify your|confirmation link|"
    r"one-time passcode|spam folder|privacy notice|terms of use|financing|"
    r"promo code|coupon|free shipping|haul away|site maintenance|store locator|"
    r"bvseo|bv_|bvpage|prod_bvrr|loc_en_|clientName_|sortentry|cookie)", re.I)
# BazaarVoice / analytics beacons rendered as list items: "y_2026, m_8, d_20, h_6"
_JUNK_BEACON = re.compile(r"^(?:[a-z][a-z0-9]{0,14}_[^,]{0,24})(?:,\s*[a-z][a-z0-9]{0,14}_[^,]{0,24}){1,}$", re.I)

_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".tif")
_BAD_IMG = re.compile(
    r"(logo|icon|sprite|placeholder|badge|banner|thumb_?nav|swatch|social|flag|"
    r"pixel|tracking|spinner|loader|arrow|star-|energy.?guide.?icon|"
    r"meganav|mega-nav|site-assets|page-content|marketing-content|/nav/|/menu/|"
    r"promo|hero-banner|footer|header|avatar|profile)", re.I)


@dataclass
class PageFacts:
    url: str
    tier: int = 1
    title: str = ""
    breadcrumbs: List[str] = field(default_factory=list)
    specs: List[Tuple[str, str, str]] = field(default_factory=list)   # (label, value, evidence_id)
    features: List[Tuple[str, str]] = field(default_factory=list)     # (text, evidence_id)
    marketing: List[Tuple[str, str]] = field(default_factory=list)
    jsonld: List[dict] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    documents: Dict[str, List[str]] = field(default_factory=dict)     # doc type -> urls
    videos: List[str] = field(default_factory=list)
    identifiers: Dict[str, str] = field(default_factory=dict)         # sku/mpn/gtin/upc/ean/price


def classify_document(url: str, anchor_text: str) -> Optional[str]:
    blob = " ".join([norm(anchor_text), norm(urlparse(url).path.replace("-", " ").replace("_", " "))])
    for doc_type, keys in DOC_PATTERNS:
        if any(k in blob for k in keys):
            return doc_type
    return None


def _clean_cell(node) -> str:
    return clean(node.get_text(" ", strip=True)) if node is not None else ""


def _plausible_pair(label: str, value: str) -> bool:
    if not label or not value:
        return False
    if len(label) > 70 or len(value) > 320:
        return False
    if _JUNK_LABEL.search(label) or _JUNK_LABEL.search(value):
        return False
    if _JUNK_TEXT.search(label) or _JUNK_TEXT.search(value):
        return False
    if "http" in value.lower() or "{}" in value or label.endswith("..."):
        return False
    # "Visit frigidaire.ca/right-to-repair for details" is a link, not a value.
    if re.search(r"\b[a-z0-9-]+\.(com|ca|net|org|co\.uk|de|fr|io)\b/?", value, re.I):
        return False
    if norm(label) == norm(value):
        return False
    return bool(re.search(r"[A-Za-z]", label))


def _is_junk_bullet(text: str) -> bool:
    return bool(_JUNK_BEACON.match(text) or _JUNK_TEXT.search(text) or _JUNK_LABEL.search(text))


def _looks_like_feature_card(label: str, value: str) -> bool:
    """A short title over a sentence of prose is a feature, not an attribute.

    Manufacturers render "3rd rack with extra wash action" + a paragraph as a
    label/value pair; the delivery format wants that title in ITEM_FEATURES_n,
    not in an ATTRIBUTE_VALUE slot.
    """
    return (len(label) <= 60 and len(value) >= 90
            and value.rstrip().endswith((".", "!"))
            and len(value.split()) >= 12)


def _emit_pair(store, facts, url, tier, label, value) -> None:
    """Route a scraped label/value into the right bucket, once."""
    if not _plausible_pair(label, value):
        return
    if _looks_like_feature_card(label, value):
        e = store.add(url, "feature", label, tier=tier, label=label, value=value,
                      doc_title=facts.title)
        facts.features.append((label, e.id if e else ""))
        d = store.add(url, "marketing", value, tier=tier, doc_title=facts.title)
        facts.marketing.append((value, d.id if d else ""))
        return
    e = store.add(url, "spec_pair", "{}: {}".format(label, value), tier=tier,
                  label=label, value=value, doc_title=facts.title)
    facts.specs.append((label, value, e.id if e else ""))


# ---------------------------------------------------------------------------
def _walk_jsonld(soup: BeautifulSoup) -> List[dict]:
    out: List[dict] = []

    def push(obj):
        if isinstance(obj, list):
            for o in obj:
                push(o)
        elif isinstance(obj, dict):
            if "@graph" in obj:
                push(obj["@graph"])
            out.append(obj)

    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
        try:
            push(json.loads(raw))
        except Exception:
            for m in re.finditer(r"\{.*\}", raw, re.S):
                try:
                    push(json.loads(m.group(0)))
                    break
                except Exception:
                    pass
    return out


def _jsonld_products(blocks: List[dict]) -> List[dict]:
    def types(o):
        t = o.get("@type", "")
        return [t] if isinstance(t, str) else list(t or [])
    return [b for b in blocks if any("product" in str(t).lower() for t in types(b))]


def _first_str(v) -> str:
    if isinstance(v, str):
        return clean(v)
    if isinstance(v, dict):
        for k in ("name", "@id", "url", "value"):
            if k in v:
                return _first_str(v[k])
    if isinstance(v, list) and v:
        return _first_str(v[0])
    return ""


# ---------------------------------------------------------------------------
def parse_html(url: str, html: str, store: EvidenceStore, tier: int = 1) -> PageFacts:
    facts = PageFacts(url=url, tier=tier)
    if not html:
        return facts
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
        if bad.name == "script" and "ld+json" in (bad.get("type") or ""):
            continue
        bad.decompose()

    if soup.title:
        facts.title = clean(soup.title.get_text(" ", strip=True))

    # --- structured data ---------------------------------------------------
    facts.jsonld = _walk_jsonld(BeautifulSoup(html, "lxml"))
    for prod in _jsonld_products(facts.jsonld):
        ev = store.add(url, "jsonld", json.dumps(prod, ensure_ascii=False)[:4000], tier=tier,
                       doc_title=facts.title)
        eid = ev.id if ev else ""
        for key, target in (("sku", "sku"), ("mpn", "mpn"), ("productID", "product_id"),
                            ("gtin", "gtin"), ("gtin12", "upc"), ("gtin13", "ean"),
                            ("gtin14", "gtin14"), ("color", "color"), ("material", "material"),
                            ("model", "model"), ("name", "name"), ("category", "category")):
            val = _first_str(prod.get(key))
            if val:
                facts.identifiers.setdefault(target, val)
        brand = _first_str(prod.get("brand"))
        if brand:
            facts.identifiers.setdefault("brand", brand)
        mfr = _first_str(prod.get("manufacturer"))
        if mfr:
            facts.identifiers.setdefault("manufacturer", mfr)
        desc = _first_str(prod.get("description"))
        if desc and len(desc) > 30:
            e = store.add(url, "marketing", desc, tier=tier, doc_title=facts.title)
            facts.marketing.append((desc, e.id if e else ""))
        offers = prod.get("offers")
        offers = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offers, dict):
            price = _first_str(offers.get("price")) or _first_str(offers.get("lowPrice"))
            if price:
                facts.identifiers.setdefault("price", price)
                facts.identifiers.setdefault("currency", _first_str(offers.get("priceCurrency")))
        for ap in (prod.get("additionalProperty") or []):
            if isinstance(ap, dict):
                lab, val = _first_str(ap.get("name")), _first_str(ap.get("value"))
                _emit_pair(store, facts, url, tier, lab, val)
        img = prod.get("image")
        for i in (img if isinstance(img, list) else [img]):
            s = _first_str(i)
            if s:
                facts.images.append(urljoin(url, s))

    # --- meta --------------------------------------------------------------
    for prop, key in (("og:title", "og_title"), ("og:description", "og_description"),
                      ("description", "meta_description"), ("og:image", "og_image")):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if not tag:
            continue
        content = clean(tag.get("content", ""))
        if not content:
            continue
        if key == "og_image":
            full = urljoin(url, content)
            if not _BAD_IMG.search(full):
                facts.images.append(full)
            continue
        e = store.add(url, "meta", content, tier=tier, label=key, doc_title=facts.title)
        if key in ("og_description", "meta_description") and len(content) > 40:
            facts.marketing.append((content, e.id if e else ""))

    # --- breadcrumbs -------------------------------------------------------
    crumbs: List[str] = []
    for block in facts.jsonld:
        if "breadcrumb" in str(block.get("@type", "")).lower():
            for el in (block.get("itemListElement") or []):
                nm = _first_str(el.get("item")) if isinstance(el, dict) else ""
                nm = _first_str(el.get("name")) if isinstance(el, dict) and not nm.startswith("http") else nm
                if isinstance(el, dict):
                    nm = _first_str(el.get("name")) or nm
                if nm and not nm.startswith("http"):
                    crumbs.append(nm)
    if not crumbs:
        nav = soup.find(attrs={"class": re.compile("breadcrumb", re.I)}) or \
              soup.find(attrs={"id": re.compile("breadcrumb", re.I)}) or \
              soup.find("nav", attrs={"aria-label": re.compile("breadcrumb", re.I)})
        if nav:
            crumbs = [clean(a.get_text(" ", strip=True)) for a in nav.find_all(["a", "li", "span"])]
    crumbs = [c for c in dict.fromkeys(crumbs) if c and 1 < len(c) < 60
              and norm(c) not in {"home", "back", "all", "shop"}]
    if crumbs:
        facts.breadcrumbs = crumbs
        store.add(url, "breadcrumb", " > ".join(crumbs), tier=tier, doc_title=facts.title)

    # --- spec tables -------------------------------------------------------
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if len(cells) < 2:
                continue
            label, value = _clean_cell(cells[0]), _clean_cell(cells[1])
            if len(cells) > 2 and not value:
                value = _clean_cell(cells[2])
            _emit_pair(store, facts, url, tier, label, value)

    # --- definition lists --------------------------------------------------
    for dl in soup.find_all("dl"):
        dts, dds = dl.find_all("dt"), dl.find_all("dd")
        for dt, dd in zip(dts, dds):
            label, value = _clean_cell(dt), _clean_cell(dd)
            _emit_pair(store, facts, url, tier, label, value)

    # --- div/li "label: value" pairs inside spec-ish containers ------------
    for container in soup.find_all(attrs={"class": _SPEC_HINT}) + soup.find_all(attrs={"id": _SPEC_HINT}):
        for node in container.find_all(["li", "div", "p", "span"], recursive=True):
            if node.find(["li", "div", "table", "ul"]):
                continue
            kids = [c for c in node.find_all(recursive=False) if c.get_text(strip=True)]
            label = value = ""
            if len(kids) == 2:
                label, value = _clean_cell(kids[0]), _clean_cell(kids[1])
            else:
                txt = _clean_cell(node)
                if ":" in txt and 4 < len(txt) < 220:
                    label, _, value = txt.partition(":")
                    label, value = clean(label), clean(value)
            _emit_pair(store, facts, url, tier, label, value)

    # --- feature bullets ---------------------------------------------------
    for head in soup.find_all(["h1", "h2", "h3", "h4", "strong", "legend"]):
        if not _FEATURE_HEAD.search(head.get_text(" ", strip=True) or ""):
            continue
        ul = head.find_next(["ul", "ol"])
        if not ul:
            continue
        for li in ul.find_all("li", recursive=True)[:24]:
            txt = _clean_cell(li)
            if 3 < len(txt) < 220 and not _is_junk_bullet(txt):
                e = store.add(url, "feature", txt, tier=tier, doc_title=facts.title)
                facts.features.append((txt, e.id if e else ""))

    # --- main marketing prose ---------------------------------------------
    try:
        import trafilatura
        body = trafilatura.extract(html, include_comments=False, include_tables=False,
                                   favor_precision=True, url=url) or ""
    except Exception:
        body = ""
    for para in [clean(p) for p in body.split("\n") if len(clean(p)) > 90][:6]:
        e = store.add(url, "marketing", para, tier=tier, doc_title=facts.title)
        facts.marketing.append((para, e.id if e else ""))

    # --- assets ------------------------------------------------------------
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src") or ""
        srcset = img.get("srcset") or img.get("data-srcset") or ""
        if srcset and not src:
            src = srcset.split(",")[-1].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            continue
        full = urljoin(url, src)
        if _BAD_IMG.search(full):
            continue
        if not any(e in full.lower().split("?")[0] for e in _IMG_EXT) and "image" not in full.lower():
            continue
        facts.images.append(full)

    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        text = clean(a.get_text(" ", strip=True))
        low = href.lower()
        if any(v in low for v in ("youtube.com/watch", "youtu.be/", "vimeo.com/", "brightcove")):
            facts.videos.append(href)
            continue
        if not (low.split("?")[0].endswith(".pdf") or "pdf" in low or "document" in low
                or "literature" in low or "/media/" in low):
            continue
        doc_type = classify_document(href, text)
        if doc_type:
            facts.documents.setdefault(doc_type, []).append(href)
            store.add(url, "asset", "{} document: {} ({})".format(doc_type, text or doc_type, href),
                      tier=tier, label=doc_type, value=href, doc_title=facts.title)

    # de-duplicate while preserving order
    facts.images = list(dict.fromkeys(facts.images))
    facts.videos = list(dict.fromkeys(facts.videos))
    for k in facts.documents:
        facts.documents[k] = list(dict.fromkeys(facts.documents[k]))
    seen = set()
    deduped = []
    for lab, val, eid in facts.specs:
        key = (norm(lab), norm(val))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((lab, val, eid))
    facts.specs = deduped
    return facts


def merge_facts(pages: List[PageFacts]) -> PageFacts:
    """Combine pages, manufacturer tier first, first-seen wins."""
    pages = sorted(pages, key=lambda p: p.tier)
    out = PageFacts(url=pages[0].url if pages else "", tier=pages[0].tier if pages else 1)
    seen_spec, seen_feat, seen_mkt = set(), set(), set()
    for p in pages:
        out.title = out.title or p.title
        out.breadcrumbs = out.breadcrumbs or p.breadcrumbs
        out.jsonld += p.jsonld
        for lab, val, eid in p.specs:
            k = (norm(lab), norm(val))
            if k not in seen_spec:
                seen_spec.add(k)
                out.specs.append((lab, val, eid))
        for txt, eid in p.features:
            if norm(txt) not in seen_feat:
                seen_feat.add(norm(txt))
                out.features.append((txt, eid))
        for txt, eid in p.marketing:
            if norm(txt) not in seen_mkt:
                seen_mkt.add(norm(txt))
                out.marketing.append((txt, eid))
        out.images += [i for i in p.images if i not in out.images]
        out.videos += [v for v in p.videos if v not in out.videos]
        for k, urls in p.documents.items():
            bucket = out.documents.setdefault(k, [])
            bucket += [u for u in urls if u not in bucket]
        for k, v in p.identifiers.items():
            out.identifiers.setdefault(k, v)
    return out
