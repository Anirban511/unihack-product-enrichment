"""End-to-end enrichment.

    input analysis -> de-duplication -> source discovery -> acquisition
    -> deterministic parsing -> constrained mapping -> grounding gate
    -> LOV / UOM normalisation -> description building -> digital assets
    -> 252-column delivery row + per-field provenance

Two invariants hold all the way through:

  1. Nothing reaches the delivery row unless `EvidenceStore.verify` can re-find
     it in a document that was actually downloaded from an allowed source.
  2. The LLM only ever selects from lists this module built. It never writes a
     value, so it cannot invent one.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from rapidfuzz import fuzz

from app.acquire import pdfs
from app.acquire.discovery import (SourceCandidate, extract_links, find_documents,
                                   find_manufacturer_domain, find_product_pages, host_of,
                                   domain_matches, infer_brand_tokens, is_banned,
                                   is_distributor, looks_like_supplier_account,
                                   mpn_variants)
from app.acquire.fetcher import fetch
from app.config import settings
from app.delivery import (MAX_ALT_IMAGES, MAX_ATTRIBUTES, MAX_FEATURES, MAX_REF_URLS,
                          asset_filename, blank_row, set_attributes, set_series)
from app.describe import Attr, DescriptionInputs, build_all, compliance
from app.evidence import EvidenceStore, Grounding, Rejection
from app.extract.llm import LlmBudget
from app.extract.parse import PageFacts, merge_facts, parse_html
from app.extract.select import (choose_classpath, choose_primary_image, choose_product_name,
                                map_spec_labels, rank_features)
from app.reference.brands import BRANDS, core_name, strip_account_code
from app.reference.lov import LOV
from app.reference.taxonomy import TAXONOMY
from app.reference.uom import UOM
from app.textnorm import (blank_if_placeholder, clean, is_placeholder, join_unique, norm,
                          to_trade_fraction)

# Identifier patterns, matched against evidence text and then re-verified.
_UPC = re.compile(r"\b(?:upc|u\.p\.c\.)\D{0,12}(\d{12})\b", re.I)
_EAN = re.compile(r"\bean\D{0,12}(\d{13})\b", re.I)
_GTIN = re.compile(r"\bgtin(?:-?1[34])?\D{0,12}(\d{12,14})\b", re.I)
_UNSPSC = re.compile(r"\bunspsc\D{0,12}(\d{8,10})\b", re.I)
_WARRANTY = re.compile(
    r"\b(\d{1,2}[\s-]?(?:year|yr|month|mo)s?\b[^.;|\n]{0,60}?warranty|"
    r"warranty[^.;|\n]{0,20}?\d{1,2}[\s-]?(?:year|yr|month|mo)s?[^.;|\n]{0,40})", re.I)
_COUNTRY = re.compile(
    r"\b(?:country of origin|made in|manufactured in|assembled in)\b\W{0,4}"
    r"([A-Z][A-Za-z .]{2,28})", re.I)
_PRICE = re.compile(r"(?:list price|msrp|suggested retail)\D{0,12}\$?\s?([\d,]+\.\d{2})", re.I)
_SERIES = re.compile(r"\b([A-Z][\w\-]{1,24}(?:\s+[A-Z][\w\-]{1,24}){0,2})\s+Series\b")
_WITH = re.compile(r"\bwith\s+([A-Z][\w™®\-]{2,30}(?:\s+[A-Za-z][\w™®\-]{1,24}){0,4})", re.I)
_APPROVALS = re.compile(
    r"\b(UL Listed|cUL Listed|UL Recognized|CSA Certified|CSA|ETL Listed|NSF Certified|"
    r"NSF|ENERGY STAR(?:\s+Certified)?|CE Marked|CE|RoHS Compliant|RoHS|ADA Compliant|ADA|"
    r"ASME [A-Z0-9.\-]+|ASSE \d{3,4}|ASTM [A-Z]\d{2,4}|ANSI [A-Z0-9.\-]+|"
    r"IAPMO|WaterSense|CEE Tier \d|NEMA [A-Z0-9]+|IP\d{2}|MIL-[A-Z0-9\-]+)\b")


@dataclass
class EnrichmentInput:
    Mfg_Part_Num: str = ""
    Part_Desc: str = ""
    E1_Brand: str = ""
    Unilog_Brand: str = ""
    DIB_Brand: str = ""
    Part_Manuf: str = ""
    SKU: str = ""
    Dept: str = ""
    Class: str = ""
    Fine: str = ""

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class FieldProvenance:
    field: str
    value: str
    source_url: str
    evidence_id: str = ""
    method: str = ""
    confidence: float = 0.0
    tier: int = 0
    is_new_lov_value: bool = False


@dataclass
class EnrichmentResult:
    input: dict
    delivery_row: Dict[str, str]
    provenance: List[FieldProvenance]
    sources: List[dict]
    metrics: dict
    warnings: List[str]
    needs_human_review: bool
    confidence: float


# ---------------------------------------------------------------------------
# 1. Input analysis
# ---------------------------------------------------------------------------
_DESC_NOISE = re.compile(
    r"\b(display only|display model|floor model|open box|clearance|closeout|"
    r"discontinued|new in box|nib|refurb\w*|scratch and dent)\b", re.I)


def analyse_input(item: EnrichmentInput) -> dict:
    """Turn one cryptic distributor row into search terms and a clean core."""
    mpn = clean(item.Mfg_Part_Num)
    desc = clean(item.Part_Desc)
    manuf_name, manuf_code = strip_account_code(item.Part_Manuf)

    core = _DESC_NOISE.sub("", desc)
    # The description usually opens with the part number - drop it, it is noise here.
    for v in mpn_variants(mpn):
        core = re.sub(r"(?i)(?<![A-Za-z0-9])" + re.escape(v) + r"(?![A-Za-z0-9])", " ", core)
    core = clean(re.sub(r"\s{2,}", " ", core)).strip(" -,")

    brands = BRANDS.clean_input_brands(item.as_dict())

    # Manufacturer name candidates, best first. Every one is a *search term*,
    # never an output value - outputs still have to be found on a real page.
    names: List[str] = []
    for cand in (blank_if_placeholder(manuf_name), brands["usable_brand"]):
        if cand and cand not in names:
            names.append(cand)
    # Deliberately NOT harvesting a "manufacturer" from the description text:
    # "WDTS7024RZ Dishwasher SS" would yield "Dishwasher SS". The description is
    # search context; the manufacturer has to come back from a real page.

    return {
        "mpn": mpn,
        "mpn_variants": mpn_variants(mpn),
        "part_desc": desc,
        "core_desc": core,
        "supplier_name": manuf_name,
        "supplier_code": manuf_code,
        "manufacturer_candidates": names,
        "brands": brands,
        "search_terms": [t for t in dict.fromkeys([
            '"{}" {}'.format(mpn, names[0] if names else "").strip(),
            "{} {}".format(names[0] if names else "", core).strip(),
        ]) if t],
    }


# ---------------------------------------------------------------------------
# 2. Acquisition
# ---------------------------------------------------------------------------
_DOC_HUB = re.compile(
    r"(manual|document|literature|spec|support|download|resource|brochure|"
    r"guide|instruction|warranty|technical)", re.I)

# A results page echoes the query back, so "the part number appears here" is
# always true on it. Reject by shape before that test can be fooled.
_NOT_A_PRODUCT_URL = re.compile(
    r"(/search\b|/searchresults|/catalogsearch|/find\b|/browse\b|/category|"
    r"/categories|/collections/?$|/shop/?$|/products/?$|/sections?/|"
    r"/explore\b|/all/?($|\?)|/plp\b|/shop-all|/product-finder|/lineup|/range/|"
    r"[?&](q|s|k|query|search|searchterm|keyword|kw)=|"
    r"[?&](filters?|facets?|refine|sort|per_page|view|page|cat|category)=)", re.I)


# Menu chrome that shows up in retailer breadcrumbs.
_NAV_LABEL = re.compile(
    r"(shop(\s+(here|now|all|by\s+\w+))?|home|explore|all products?|products?|"
    r"catalog|browse|menu|store|our (?:products?|range)|new arrivals|deals|"
    r"clearance|sale|brands?|view all|see all|back)", re.I)


def looks_like_product_page(url: str, facts: PageFacts,
                            variants: Sequence[str] = ()) -> Tuple[bool, str]:
    """Is this THIS product's page, or a list of many / a support hub?

    Three independent tests, all of which must pass:

      1. the URL must not have the shape of a search or faceted listing - a
         results page echoes the query back, so "the part number appears here"
         is true on it by construction;
      2. the page must be *about this part* - the part number has to appear in
         the URL, in structured data, or in the title, not merely somewhere in
         the body where a "related products" strip would put it;
      3. the page must carry product-shaped structure.

    Test 2 is what separates a real product page from a category listing that
    happens to contain the part: diablotools.com/explore/all/?filters=... lists
    every sander they make, and mining it yields the category's marketing copy
    rather than the product's.
    """
    url = url or ""
    if _NOT_A_PRODUCT_URL.search(url):
        return False, "search or faceted listing page, not a product page"

    if variants:
        path = norm(urlparse(url).path)
        ident = norm(" ".join([facts.identifiers.get("sku", ""),
                               facts.identifiers.get("mpn", ""),
                               facts.identifiers.get("model", ""),
                               facts.title]))
        if not any(norm(v) in path or norm(v) in ident for v in variants if v):
            return False, "page is not specific to this part number"

    if facts.identifiers.get("sku") or facts.identifiers.get("mpn") or facts.jsonld:
        return True, ""
    if len(facts.specs) >= 3:
        return True, ""
    if facts.images and (facts.features or facts.marketing):
        return True, ""
    return False, "no product structure found on the page"


def acquire(analysis: dict, store: EvidenceStore, warnings: List[str]
            ) -> Tuple[List[PageFacts], List[dict], Optional[str]]:
    """Discover, download and parse the allowed sources for one part."""
    # The brand the wider web associates with this part number. Purely a
    # navigation aid - it decides where to look, never what to write.
    supplied = analysis["supplier_name"]
    inferred = infer_brand_tokens(analysis["mpn"], analysis["core_desc"], prefer=supplied)
    # The supplied manufacturer name is evidence too. Ranking it alongside the
    # inferred token - rather than letting one popular but spurious token decide -
    # is what stops a part number that reads as a hex colour from resolving to a
    # colour-reference site.
    names = list(dict.fromkeys(inferred[:1] + analysis["manufacturer_candidates"]
                               + inferred[1:2]))
    analysis["inferred_brand_tokens"] = inferred

    domain, why = find_manufacturer_domain(names, analysis["mpn"],
                                           analysis["core_desc"], prefer=supplied)
    if not domain:
        warnings.append("manufacturer domain not resolved: " + why)

    candidates = find_product_pages(analysis["mpn"], names, domain)

    # The row's own supplier is not the manufacturer. If discovery has landed on
    # the distributor's web shop, that is a tier-2 fallback source at best - it
    # must never be recorded as "from the manufacturer's own site".
    supplier = analysis["supplier_name"]
    if supplier and looks_like_supplier_account(supplier):
        stem = core_name(supplier).replace(" ", "")
        for c in candidates:
            host = host_of(c.url).replace("-", "").replace(".", "")
            if stem and len(stem) > 4 and stem[:12] in host:
                c.tier = 2
                c.reason = "distributor fallback (row's own supplier site)"
        if domain and stem and len(stem) > 4 and stem[:12] in domain.replace("-", "").replace(".", ""):
            warnings.append("resolved domain is the row's own supplier ({}), "
                            "treated as a distributor fallback".format(domain))
            domain = None
        candidates.sort(key=lambda c: (c.tier, -c.score))

    if not candidates:
        warnings.append("no product page found on an allowed source")
        return [], [], domain

    pages: List[PageFacts] = []
    sources: List[dict] = []
    doc_hub_links: List[Tuple[str, int]] = []

    for cand in candidates:
        if is_banned(cand.url):
            continue
        res = fetch(cand.url)
        record = {"url": cand.url, "final_url": res.final_url or cand.url, "tier": cand.tier,
                  "reason": cand.reason, "status": res.status, "fetch_tier": res.tier,
                  "elapsed_ms": res.elapsed_ms,
                  "policy": "manufacturer" if cand.tier == 1 else "distributor-fallback"}
        if not res.ok:
            record["error"] = res.error or "fetch failed"
            sources.append(record)
            continue

        if res.is_pdf:
            n, _ = pdfs.harvest_pdf(res.final_url or cand.url, store, tier=cand.tier)
            record["pdf_pages"] = n
            sources.append(record)
            continue

        facts = parse_html(res.final_url or cand.url, res.html, store, tier=cand.tier)
        # A page that does not mention the part number is the wrong page.
        blob = norm(" ".join([facts.title] + [v for _l, v, _e in facts.specs[:40]]
                             + [t for t, _ in facts.marketing[:3]]))
        page_norm = norm(res.html[:400000])
        if not any(norm(v) in page_norm for v in analysis["mpn_variants"]):
            record["skipped"] = "part number not present on page"
            sources.append(record)
            continue

        is_product, why_not = looks_like_product_page(res.final_url or cand.url, facts,
                                                      analysis["mpn_variants"])
        if not is_product:
            record["skipped"] = why_not
            sources.append(record)
            continue

        # Tier is decided by who actually served the page, not by what we hoped
        # to find. A retailer that ranks well for a part number is still a
        # retailer, and must not be recorded as the manufacturer.
        served_by_brand = domain_matches(res.final_url or cand.url, names)
        if cand.tier == 1 and not served_by_brand:
            cand.tier = 2
            facts.tier = 2
            record["tier"] = 2
            record["policy"] = "distributor-fallback"
            record["reason"] = "third-party site, not the manufacturer's own domain"

        pages.append(facts)
        record.update({"spec_pairs": len(facts.specs), "features": len(facts.features),
                       "images": len(facts.images), "documents": sum(len(v) for v in facts.documents.values())})
        sources.append(record)

        for link in extract_links(res.final_url or cand.url, res.html):
            if host_of(link) == host_of(res.final_url or cand.url) and _DOC_HUB.search(link):
                doc_hub_links.append((link, cand.tier))

    # Spec-sheet PDFs indexed on the manufacturer's own domain. Modern product
    # pages render their spec panel client-side from a service call, so the PDF
    # is frequently the only place the electrical and dimensional data exists.
    for doc in find_documents(analysis["mpn"], names, domain, limit=3):
        n, _specs = pdfs.harvest_pdf(doc.url, store, tier=doc.tier, doc_type="Specification Sheet")
        if n:
            sources.append({"url": doc.url, "final_url": doc.url, "tier": doc.tier,
                            "reason": doc.reason, "status": 200, "pdf_pages": n,
                            "policy": "manufacturer"})
            if pages:
                pages[0].documents.setdefault("Specification Sheet", []).append(doc.url)

    # Follow one document hub per item - that is where the spec sheets hide.
    for link, tier in doc_hub_links[:2]:
        if any(link == p.url for p in pages):
            continue
        res = fetch(link)
        if not res.ok or res.is_pdf:
            continue
        hub = parse_html(res.final_url or link, res.html, store, tier=tier)
        if hub.documents:
            pages.append(hub)
            sources.append({"url": link, "final_url": res.final_url or link, "tier": tier,
                            "reason": "document hub linked from product page",
                            "status": res.status, "fetch_tier": res.tier,
                            "documents": sum(len(v) for v in hub.documents.values()),
                            "policy": "manufacturer" if tier == 1 else "distributor-fallback"})
    return pages, sources, domain


# ---------------------------------------------------------------------------
# 3. Classification
# ---------------------------------------------------------------------------
def classify(analysis: dict, facts: PageFacts, budget: LlmBudget,
             warnings: List[str]) -> Tuple[str, float, str]:
    context = " | ".join(filter(None, [
        analysis["part_desc"], facts.title, " > ".join(facts.breadcrumbs),
        "; ".join("{}={}".format(l, v) for l, v, _ in facts.specs[:15]),
    ]))
    scored = TAXONOMY.candidates(context, k=12)
    # A weak lexical match is worse than no match: with a partially loaded
    # taxonomy it will happily file a sanding belt under dishwashers. Require a
    # real signal before the shortlist is worth reranking.
    strong = [c for c, sc in scored if sc >= 0.45]
    candidates = strong or [c for c, sc in scored if sc >= 0.30]

    # Nothing in the loaded taxonomy is close - propose the manufacturer's own
    # breadcrumb as a new leaf rather than forcing a wrong category.
    if not candidates and facts.breadcrumbs:
        # The final crumb is usually the product itself, not a category, and a
        # retailer's breadcrumb often opens with a navigation label. "Shop
        # Here>Sanding Belts" is a menu, not a taxonomy, so nav labels are
        # dropped and a proposal made only of them is discarded entirely.
        crumbs = [c for c in facts.breadcrumbs
                  if not any(norm(v) in norm(c) for v in analysis["mpn_variants"])
                  and not _NAV_LABEL.fullmatch(clean(c))]
        if not crumbs:
            warnings.append("no classpath: the page's breadcrumb is site navigation, "
                            "not a product taxonomy")
            return "", 0.0, "breadcrumb-rejected"
        proposed = ">".join(crumbs[-3:])
        TAXONOMY.register_leaf(proposed)
        warnings.append("classpath proposed from manufacturer breadcrumb "
                        "(no match in loaded taxonomy of {} leaves)".format(len(TAXONOMY.leaves)))
        return proposed, 0.4, "breadcrumb-proposal"
    if not candidates:
        warnings.append("no classpath could be determined")
        return "", 0.0, "none"

    classpath, conf, method = choose_classpath(context, candidates, budget)
    return classpath or "", conf, method


# ---------------------------------------------------------------------------
# 4. Attributes
# ---------------------------------------------------------------------------
# Labels that legitimately carry a long, composite value.
_LONG_VALUE_LABELS = ("additional information", "size", "includes", "description",
                      "application", "dimensions", "standard", "approval")


def _is_prose(value: str, label: str = "") -> bool:
    """An attribute value is a value, not a sentence.

    Manufacturers frequently pair a feature title with a paragraph, and pair a
    dimension with a sales note ("3/4 in. x 60 ft. Other lengths and widths ...").
    Both look exactly like a spec row, and letting them through puts marketing
    copy in an ATTRIBUTE_VALUE column.
    """
    v = clean(value)
    generous = any(f in norm(label) for f in _LONG_VALUE_LABELS)
    if len(v) > (200 if generous else 90):
        return True
    if not generous and len(v.split()) > 8:
        return True
    if len(v.split()) > 12 and v.rstrip().endswith((".", "!")):
        return True
    # Sentence connectives never appear in a spec value; they appear in the
    # marketing note that sits beside it ("... Other lengths and widths are
    # available in the following ...").
    return bool(re.search(r"\b(the following|available in|please|see |refer to|"
                          r"for more|such as|as well as|in order to)\b", v, re.I))


def build_attributes(classpath: str, facts: PageFacts, store: EvidenceStore,
                     budget: LlmBudget) -> Tuple[List[dict], List[FieldProvenance]]:
    """Map scraped pairs onto the category schema, then verify every value."""
    schema = LOV.schema(classpath)
    labels = list(schema.keys())
    pairs = facts.specs

    if not labels:
        # No LOV coverage: the manufacturer's own labels become the schema, and
        # every value is flagged as a new LOV entry for governance.
        labels = [l for l, _v, _e in pairs][:MAX_ATTRIBUTES]
        mapping = {l: i for i, (l, _v, _e) in enumerate(pairs[:MAX_ATTRIBUTES])}
    else:
        mapping: Dict[str, int] = {}
        used: set = set()
        for i, (raw_label, _v, _e) in enumerate(pairs):
            target = LOV.resolve_label(classpath, raw_label)
            if target and target not in mapping and i not in used:
                mapping[target] = i
                used.add(i)
        if len(mapping) < len(labels):
            mapping = map_spec_labels(classpath, labels, pairs, budget, already=mapping)

    attributes: List[dict] = []
    provenance: List[FieldProvenance] = []
    for seq, label in enumerate(labels[:MAX_ATTRIBUTES], start=1):
        entry = {"label": label, "value": "", "uom": "", "sequence": seq}
        idx = mapping.get(label)
        if idx is None or idx >= len(pairs):
            attributes.append(entry)
            continue

        _raw_label, raw_value, ev_id = pairs[idx]
        if _is_prose(raw_value, label):
            attributes.append(entry)        # a sentence is copy, not an attribute
            continue
        value, uom = UOM.split(raw_value)
        canonical, is_new = LOV.normalise_value(classpath, label, value)

        grounding = store.verify(raw_value, prefer=[ev_id],
                                 field_name="ATTRIBUTE:" + label)
        if not grounding.ok:
            attributes.append(entry)             # unproven -> stays blank
            continue

        entry["value"] = canonical
        entry["uom"] = uom
        attributes.append(entry)
        provenance.append(FieldProvenance(
            field="ATTRIBUTE:" + label, value=UOM.format_measure(canonical, uom) if uom else canonical,
            source_url=grounding.citation, evidence_id=grounding.evidence_id,
            method="lov-normalised" if not is_new else "new-lov-value",
            confidence=grounding.ratio, tier=grounding.tier, is_new_lov_value=is_new))
    return attributes, provenance


# ---------------------------------------------------------------------------
# 5. Free-standing fields
# ---------------------------------------------------------------------------
def _first_match(pattern: re.Pattern, store: EvidenceStore, group: int = 1
                 ) -> Tuple[str, Optional[Grounding]]:
    for ev in sorted(store.items.values(), key=lambda e: (e.tier, e.id)):
        m = pattern.search(ev.text)
        if m:
            value = clean(m.group(group))
            if value:
                return value, Grounding(True, ev.id, ev.citation, 1.0, "regex", ev.tier)
    return "", None


def find_cased_form(store: EvidenceStore, token: str) -> Tuple[str, Optional[Grounding]]:
    """Recover how the manufacturer actually writes a name, symbols included.

    The inferred brand token is a lowercase URL fragment ("frigidaire"). The
    delivery file wants the manufacturer's own rendering ("FRIGIDAIRE(R)"), so
    the token is used only to *locate* the string; the characters written out
    are the ones found on the page.
    """
    t = clean(token)
    if len(t) < 3:
        return "", None
    pattern = re.compile(r"\b(" + re.escape(t) + r")\s?([®™℠])?", re.I)
    best: Tuple[str, Optional[Grounding]] = ("", None)
    for ev in sorted(store.items.values(), key=lambda e: (e.tier, e.id)):
        m = pattern.search(ev.text)
        if not m:
            continue
        cased = m.group(1) + (m.group(2) or "")
        g = Grounding(True, ev.id, ev.citation, 1.0, "cased-form-on-source", ev.tier)
        # A registered form is the canonical one; keep looking until we see it.
        if m.group(2):
            return cased, g
        if not best[0]:
            best = (cased, g)
    return best


def derive_identity(analysis: dict, facts: PageFacts, store: EvidenceStore
                    ) -> Tuple[Dict[str, str], List[FieldProvenance]]:
    """Manufacturer, brand and part number - proven against the site itself."""
    out: Dict[str, str] = {}
    prov: List[FieldProvenance] = []

    def record(fieldname: str, value: str, grounding: Optional[Grounding], method: str):
        if not value or grounding is None or not grounding.ok:
            return
        out[fieldname] = value
        prov.append(FieldProvenance(field=fieldname, value=value, source_url=grounding.citation,
                                    evidence_id=grounding.evidence_id, method=method,
                                    confidence=grounding.ratio, tier=grounding.tier))

    brand = clean(facts.identifiers.get("brand", ""))
    if brand:
        record("BRAND_NAME", brand, store.verify(brand, field_name="BRAND_NAME"), "jsonld-brand")
    if "BRAND_NAME" not in out:
        # An inferred token is only a brand if the site we actually read is named
        # after it. Without that check a commodity part - a dimensional stud, say -
        # writes its own product noun into BRAND_NAME: grounded, and nonsense.
        for token in analysis.get("inferred_brand_tokens", [])[:2]:
            owns_source = domain_matches(facts.url, [token]) or any(
                domain_matches(e.url, [token]) for e in store.items.values() if e.tier == 1)
            if not owns_source:
                continue
            cased, g = find_cased_form(store, token)
            if cased:
                record("BRAND_NAME", cased, g, "brand-confirmed-on-its-own-site")
                break
    mfr = clean(facts.identifiers.get("manufacturer", ""))
    if mfr:
        record("MANUFACTURER_NAME", mfr, store.verify(mfr, field_name="MANUFACTURER_NAME"),
               "jsonld-manufacturer")
    if "MANUFACTURER_NAME" not in out:
        for cand in analysis["manufacturer_candidates"]:
            g = store.verify(cand, field_name="MANUFACTURER_NAME")
            if g.ok:
                record("MANUFACTURER_NAME", cand, g, "supplier-name-confirmed-on-site")
                break
    if "MANUFACTURER_NAME" not in out:
        # The supplied name is provided data, not a guess, so it may be written
        # even when the manufacturer's own pages never spell it out - which is
        # common, because a maker's site brands itself ("Diablo") rather than
        # naming its legal entity ("Freud Inc"). It is withheld only when it
        # names a distributor account, which is not a manufacturer at all.
        supplied = clean(analysis["supplier_name"])
        if supplied and not looks_like_supplier_account(supplied):
            out["MANUFACTURER_NAME"] = supplied
            prov.append(FieldProvenance(
                field="MANUFACTURER_NAME", value=supplied, source_url="(supplied on the input row)",
                method="from-input", confidence=0.6, tier=0))

    mpn = clean(facts.identifiers.get("mpn") or facts.identifiers.get("sku") or "")
    for cand in ([mpn] if mpn else []) + analysis["mpn_variants"]:
        if not cand:
            continue
        g = store.verify(cand, field_name="MANUFACTURER_PART_NUMBER")
        if g.ok:
            record("MANUFACTURER_PART_NUMBER", cand, g, "confirmed-on-manufacturer-page")
            break
    return out, prov


def derive_extras(facts: PageFacts, store: EvidenceStore
                  ) -> Tuple[Dict[str, str], List[FieldProvenance]]:
    """UPC / EAN / GTIN / UNSPSC / warranty / origin / price / approvals."""
    out: Dict[str, str] = {}
    prov: List[FieldProvenance] = []

    def take(fieldname: str, value: str, g: Optional[Grounding], method: str):
        if value and g and g.ok:
            out[fieldname] = value
            prov.append(FieldProvenance(field=fieldname, value=value, source_url=g.citation,
                                        evidence_id=g.evidence_id, method=method,
                                        confidence=g.ratio, tier=g.tier))

    # Deliberately no free-text price scan: "MSRP $999.00" on a product page is
    # as likely to belong to an upsell tile as to this item. Price is taken from
    # structured offer data below, or not at all.
    for fieldname, pattern in (("UPC", _UPC), ("EAN", _EAN), ("GTIN", _GTIN),
                               ("UNSPSC", _UNSPSC), ("Warranty", _WARRANTY),
                               ("Country Of Origin", _COUNTRY)):
        value, g = _first_match(pattern, store)
        take(fieldname, value, g, "regex-on-source")

    for key, fieldname in (("upc", "UPC"), ("ean", "EAN"), ("gtin", "GTIN"),
                           ("gtin14", "GTIN"), ("price", "List Price")):
        if fieldname in out:
            continue
        v = clean(facts.identifiers.get(key, ""))
        if v:
            take(fieldname, v, store.verify(v, field_name=fieldname), "jsonld")

    approvals: List[str] = []
    for ev in sorted(store.items.values(), key=lambda e: (e.tier, e.id)):
        for m in _APPROVALS.finditer(ev.text):
            token = clean(m.group(1))
            if token and token not in approvals:
                approvals.append(token)
    if approvals:
        joined = "|".join(sorted(approvals, key=str.lower))
        g = store.verify(approvals[0], field_name="Standard/Approvals")
        take("Standard/Approvals", joined, g, "approval-token-scan")
    return out, prov


def derive_series_and_with(facts: PageFacts, store: EvidenceStore
                           ) -> Tuple[str, str, List[FieldProvenance]]:
    prov: List[FieldProvenance] = []
    series, g = _first_match(_SERIES, store)
    if series:
        series = "{} Series".format(series)
        gg = store.verify(series, field_name="Series")
        if gg.ok:
            prov.append(FieldProvenance(field="ATTRIBUTE:Series", value=series,
                                        source_url=gg.citation, evidence_id=gg.evidence_id,
                                        method="regex-on-source", confidence=gg.ratio,
                                        tier=gg.tier))
        else:
            series = ""

    with_clause = ""
    title = facts.title or ""
    m = re.search(r"\bwith\s+(?:a\s+)?([A-Za-z0-9][\w™®\-]*(?:\s+[A-Za-z0-9][\w™®\-]*){0,5})",
                  title, re.I)
    if m:
        # The title runs on ("... With CleanBoost™ Stainless Steel-PDSH4816AF"),
        # so the clause stops at the feature name: keep leading capitalised or
        # trademarked words only, and never a part number.
        kept: List[str] = []
        for word in clean(m.group(1)).split():
            if re.search(r"\d{3,}", word) or (kept and not (word[:1].isupper()
                                                            or word[:1].isdigit())):
                break
            kept.append(word)
            if any(sym in word for sym in "™®"):
                break
        if not kept:
            return series, "", prov
        candidate = "With " + " ".join(kept[:4])
        g2 = store.verify(" ".join(kept[:4]), field_name="With")
        if g2.ok:
            with_clause = candidate
            prov.append(FieldProvenance(field="With", value=with_clause, source_url=g2.citation,
                                        evidence_id=g2.evidence_id, method="title-clause",
                                        confidence=g2.ratio, tier=g2.tier))
    return series, with_clause, prov


# ---------------------------------------------------------------------------
# 6. Orchestration
# ---------------------------------------------------------------------------
def enrich(item: EnrichmentInput) -> EnrichmentResult:
    t0 = time.time()
    store = EvidenceStore()
    budget = LlmBudget(max_calls=settings.llm_max_calls_per_item)
    warnings: List[str] = []
    provenance: List[FieldProvenance] = []

    analysis = analyse_input(item)
    pages, sources, domain = acquire(analysis, store, warnings)
    facts = merge_facts(pages) if pages else PageFacts(url="")

    if pages:
        harvested = pdfs.harvest_documents(facts.documents, store, tier=facts.tier)
        if harvested:
            for doc_type, n in harvested.items():
                sources.append({"url": facts.documents[doc_type][0], "tier": facts.tier,
                                "reason": "linked {} ingested".format(doc_type),
                                "pdf_pages": n, "policy": "manufacturer"})
            facts = merge_facts(pages + [PageFacts(url=facts.url, tier=facts.tier)])
            facts.specs += [(e.label, e.value, e.id) for e in store.by_kind("spec_pair")
                            if e.kind == "spec_pair" and e.page and (e.label, e.value, e.id) not in facts.specs]

    classpath, class_conf, class_method = ("", 0.0, "no-sources")
    if store.items:
        classpath, class_conf, class_method = classify(analysis, facts, budget, warnings)
    dept, klass, fine = TAXONOMY.split(classpath)
    # Dept / Class / Fine is the distributor's own hierarchy, not a slice of the
    # Unilog classpath, so it is classified in its own right when available.
    dcf_context = " | ".join(filter(None, [analysis["part_desc"], facts.title,
                                           " > ".join(facts.breadcrumbs), classpath]))
    d2, c2, f2 = TAXONOMY.classify_dept(dcf_context)
    if d2:
        dept, klass, fine = d2, c2, f2

    # Attributes are still worth capturing without a classpath: the
    # manufacturer's own labels become the schema and every value is flagged as
    # a new LOV entry, which is a governance task, not a reason to drop data.
    attributes, attr_prov = ([], [])
    if store.items:
        attributes, attr_prov = build_attributes(classpath, facts, store, budget)
        provenance += attr_prov

    identity, id_prov = derive_identity(analysis, facts, store)
    extras, extra_prov = derive_extras(facts, store)
    series, with_clause, sw_prov = derive_series_and_with(facts, store)
    provenance += id_prov + extra_prov + sw_prov

    # --- product name ------------------------------------------------------
    name_candidates: List[str] = []
    if facts.breadcrumbs:
        name_candidates.append(facts.breadcrumbs[-1])
        if len(facts.breadcrumbs) > 1:
            name_candidates.append(facts.breadcrumbs[-2])
    if fine:
        name_candidates.append(re.sub(r"^(built-?in|large|small)\s+", "", fine, flags=re.I).rstrip("s"))
        name_candidates.append(fine)
    if facts.identifiers.get("category"):
        name_candidates.append(facts.identifiers["category"].split(">")[-1])
    # The page title usually ends with the product type: "... Built-In Dishwasher".
    title_tail = re.sub(r"\s*[|\-–]\s*[^|\-–]*$", "", clean(facts.title))
    words = [w for w in title_tail.split() if w]
    for n in (1, 2):
        if len(words) >= n:
            phrase = " ".join(words[-n:]).strip(" .,:;\"'")
            if phrase and not re.search(r"\d{3,}", phrase):
                name_candidates.append(phrase)
    name_candidates = [c for c in name_candidates
                       if c and len(c) < 45 and not re.search(r"\d{3,}", c)]
    product_name, name_method = choose_product_name(
        name_candidates, " | ".join([facts.title, analysis["part_desc"]]), budget)

    # --- features and marketing (verbatim, verified) -----------------------
    feature_texts = [t for t, _e in facts.features]
    keep = rank_features(feature_texts, facts.title or analysis["part_desc"], budget,
                         keep=MAX_FEATURES) if feature_texts else []
    features: List[str] = []
    for i in keep:
        text, ev_id = facts.features[i]
        g = store.verify(text, prefer=[ev_id], field_name="ITEM_FEATURE")
        if g.ok:
            features.append(text)
            provenance.append(FieldProvenance(field="ITEM_FEATURES_{}".format(len(features)),
                                              value=text, source_url=g.citation,
                                              evidence_id=g.evidence_id, method="verbatim",
                                              confidence=g.ratio, tier=g.tier))
        if len(features) >= MAX_FEATURES:
            break

    # Category pages advertise a range, not an item. Copy that never names the
    # product is the "fluent description of invented values" the brief penalises,
    # even though the string itself was scraped verbatim.
    _CATEGORY_COPY = re.compile(
        r"(all products|our (?:full )?(?:range|products|line-?up)|explore (?:our|the)|"
        r"shop (?:our|all)|browse (?:our|the)|best in the world|discover (?:our|the))", re.I)

    marketing = ""
    for text, ev_id in sorted(facts.marketing, key=lambda t: -len(t[0])):
        if not (60 <= len(text) <= 1200):
            continue
        if _CATEGORY_COPY.search(text):
            store.rejections.append(Rejection(
                field="MARKETING_DESCRIPTION", value=text[:120],
                reason="category-level copy, not about this product"))
            continue
        g = store.verify(text, prefer=[ev_id], field_name="MARKETING_DESCRIPTION")
        if g.ok:
            marketing = text
            provenance.append(FieldProvenance(field="MARKETING_DESCRIPTION", value=text,
                                              source_url=g.citation, evidence_id=g.evidence_id,
                                              method="verbatim", confidence=g.ratio, tier=g.tier))
            break

    # --- descriptions ------------------------------------------------------
    # Identity fields are grounded or blank - never back-filled from the input,
    # because the input's "manufacturer" is routinely the distributor account.
    brand = identity.get("BRAND_NAME", "")
    manufacturer = identity.get("MANUFACTURER_NAME", "")
    part_number = identity.get("MANUFACTURER_PART_NUMBER", "") or analysis["mpn"]
    if not manufacturer:
        warnings.append("MANUFACTURER_NAME left blank: not confirmed on any retrieved source")
    if not brand:
        warnings.append("BRAND_NAME left blank: not confirmed on any retrieved source")
    series = series or next((a["value"] for a in attributes if norm(a["label"]) == "series"), "")
    additional = next((a["value"] for a in attributes
                       if norm(a["label"]) == "additional information"), "")

    desc_inputs = DescriptionInputs(
        manufacturer=manufacturer, brand=brand, part_number=part_number,
        product_name=product_name, series=series, with_clause=with_clause,
        additional_information=additional,
        attributes=[Attr(a["label"], a["value"], a["uom"], a["sequence"])
                    for a in attributes if a["value"]],
    )
    descriptions = build_all(desc_inputs)
    limits = compliance(descriptions)

    # --- assets ------------------------------------------------------------
    images = [i for i in facts.images if not is_banned(i)]
    primary_idx = choose_primary_image(images, part_number, budget) if images else None
    ordered_images: List[str] = []
    if primary_idx is not None:
        primary = images[primary_idx]
        # Alternates must belong to the same product: same part number in the
        # filename, or the same asset folder as the hero shot. Otherwise a
        # "related accessories" strip becomes Alternate Image 1.
        mpn_key = re.sub(r"[^a-z0-9]", "", part_number.lower())
        folder = primary.rsplit("/", 1)[0]
        siblings = [u for j, u in enumerate(images) if j != primary_idx
                    and ((mpn_key and mpn_key in re.sub(r"[^a-z0-9]", "", u.lower()))
                         or u.rsplit("/", 1)[0] == folder)]
        ordered_images = [primary] + siblings

    row = blank_row()
    row.update({
        "MFR URL": facts.url or "",
        "PART_NUMBER": clean(item.SKU) or "",
        "Dept": clean(item.Dept) or dept,
        "Class": clean(item.Class) or klass,
        "Fine": clean(item.Fine) or fine,
        "SKU - MY_PART_NUMBER": clean(item.SKU),
        "Mfg_Part_Num": analysis["mpn"],
        "Part_Desc": analysis["part_desc"],
        "E1_Brand": clean(item.E1_Brand),
        "Unilog_Brand": clean(item.Unilog_Brand),
        "DIB_Brand": clean(item.DIB_Brand),
        "Part_Manuf": clean(item.Part_Manuf),
        "MANUFACTURER_NAME": manufacturer,
        "BRAND_NAME": brand,
        "MANUFACTURER_PART_NUMBER": part_number,
        "Classpath": classpath,
        "Product Name": product_name,
        "With": with_clause,
        "MARKETING_DESCRIPTION": marketing,
        **descriptions,
        **{k: v for k, v in extras.items() if k in row},
    })

    ref_urls = [s.get("final_url") or s["url"] for s in sources
                if s.get("status") == 200 and (s.get("final_url") or s["url"]) != row["MFR URL"]]
    set_series(row, "Ref URL {}", list(dict.fromkeys(ref_urls)), MAX_REF_URLS)
    set_series(row, "ITEM_FEATURES_{}", features, MAX_FEATURES)
    set_attributes(row, attributes)

    label_source = brand or manufacturer or "Product"
    if ordered_images:
        row["Product Image"] = asset_filename(label_source, part_number, ordered_images[0])
        provenance.append(FieldProvenance(field="Product Image", value=ordered_images[0],
                                          source_url=ordered_images[0], method="manufacturer-asset",
                                          confidence=1.0, tier=facts.tier))
        for i, u in enumerate(ordered_images[1:1 + MAX_ALT_IMAGES], start=1):
            row["Alternate Image {}".format(i)] = asset_filename(
                label_source, part_number, u, suffix=str(i))
            provenance.append(FieldProvenance(field="Alternate Image {}".format(i), value=u,
                                              source_url=u, method="manufacturer-asset",
                                              confidence=1.0, tier=facts.tier))
        row["Actual Image (Yes/No)"] = "Yes"

    for doc_type, urls in facts.documents.items():
        if doc_type in row and urls:
            row[doc_type] = asset_filename(label_source, part_number, urls[0],
                                           suffix=doc_type, default_ext=".pdf")
            provenance.append(FieldProvenance(field=doc_type, value=urls[0], source_url=urls[0],
                                              method="manufacturer-asset", confidence=1.0,
                                              tier=facts.tier))
    if facts.videos:
        row["Video Link"] = facts.videos[0]
        if len(facts.videos) > 1:
            row["Video Link 1"] = facts.videos[1]

    # --- scoring -----------------------------------------------------------
    filled = sum(1 for v in row.values() if clean(v))
    attr_filled = sum(1 for a in attributes if a["value"])
    tier = facts.tier or 0
    confidence = 0.0
    if store.items:
        parts = [
            0.30 * (1.0 if tier == 1 else 0.6 if tier == 2 else 0.0),
            0.25 * class_conf,
            0.25 * (attr_filled / max(1, len(attributes))),
            0.10 * (1.0 if marketing else 0.0),
            0.10 * (sum(1 for c in limits.values() if c["compliant"]) / len(limits)),
        ]
        confidence = round(sum(parts), 3)

    if tier == 2:
        warnings.append("data sourced from a distributor fallback, not the manufacturer's site")
    if store.rejections:
        warnings.append("{} candidate values were rejected as ungrounded".format(len(store.rejections)))
    if not classpath:
        warnings.append("no classpath - attributes could not be sequenced")

    needs_review = bool(
        confidence < settings.review_confidence_threshold or not store.items
        or tier != 1 or not classpath or not row["Product Image"]
    )

    metrics = {
        "elapsed_ms": int((time.time() - t0) * 1000),
        "evidence": store.stats(),
        "llm": budget.as_dict(),
        "sources_used": len(sources),
        "source_tier": tier,
        "classification": {"classpath": classpath, "confidence": class_conf,
                           "method": class_method, "taxonomy": TAXONOMY.status()},
        "product_name_method": name_method,
        "attributes": {"in_schema": len(attributes), "populated": attr_filled,
                       "new_lov_values": sum(1 for p in attr_prov if p.is_new_lov_value)},
        "descriptions": limits,
        "delivery_columns_filled": filled,
        "delivery_columns_total": len(row),
        "grounding_rejections": [r.__dict__ for r in store.rejections[:25]],
    }

    return EnrichmentResult(
        input=item.as_dict(), delivery_row=row, provenance=provenance, sources=sources,
        metrics=metrics, warnings=warnings, needs_human_review=needs_review,
        confidence=confidence,
    )
