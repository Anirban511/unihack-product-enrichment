"""Find the manufacturer's own site, then the product page on it.

Sourcing policy is enforced here rather than downstream, so a banned domain can
never enter the evidence store in the first place:

  tier 1  manufacturer's own domain            (always preferred)
  tier 2  reputed industrial distributor       (only if tier 1 yields nothing)
  never   consumer marketplaces                (Amazon, eBay, Walmart, ...)
"""
from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from rapidfuzz import fuzz

from app.acquire.fetcher import fetch
from app.config import settings
from app.reference.brands import core_name, strip_account_code
from app.textnorm import clean, norm

_GENERIC_HOSTS = (
    "wikipedia.org", "linkedin.com", "bloomberg.com", "crunchbase.com", "youtube.com",
    "glassdoor.com", "indeed.com", "zoominfo.com", "dnb.com", "manualslib.com",
    "google.", "bing.com", "duckduckgo.com", "yahoo.", "yandex.", "archive.org",
    "manualsonline.com", "scribd.com", "issuu.com", "slideshare.net", "twitter.com",
    "x.com", "tiktok.com", "blogspot.", "wordpress.com", "medium.com",
)


@dataclass
class SourceCandidate:
    url: str
    tier: int          # 1 manufacturer, 2 distributor
    reason: str
    score: float = 0.0


def registrable(host: str) -> str:
    host = (host or "").lower().lstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def host_of(url: str) -> str:
    return registrable(urlparse(url).netloc)


def is_banned(url: str) -> bool:
    h = host_of(url)
    return any(b.rstrip(".") in h for b in settings.banned_domains)


def is_distributor(url: str) -> bool:
    h = host_of(url)
    return any(d in h for d in settings.distributor_domains)


def is_generic(url: str) -> bool:
    h = host_of(url)
    return any(g.rstrip(".") in h for g in _GENERIC_HOSTS)


# ---------------------------------------------------------------------------
# Keyless web search
# ---------------------------------------------------------------------------
_DDG_LINK = re.compile(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"', re.I)
_DDG_LITE = re.compile(r'<a[^>]+href="(https?://[^"]+)"[^>]*class="result-link"', re.I)
_ANY_LINK = re.compile(r'href="(/l/\?[^"]*uddg=[^"]+|https?://[^"]+)"', re.I)


def _unwrap(href: str) -> str:
    href = htmllib.unescape(href)
    if "uddg=" in href:
        qs = parse_qs(urlparse(href if href.startswith("http") else "https://duckduckgo.com" + href).query)
        if qs.get("uddg"):
            return unquote(qs["uddg"][0])
    return href


def web_search(query: str, limit: int = 12) -> List[str]:
    """Keyless SERP scrape. Returns de-duplicated result URLs, policy-filtered.

    The plain-HTTP SERP endpoints answer 202 + an anti-bot page, so the fetcher's
    browser tier does the work here; results are disk-cached so a query is paid
    for once per week, not once per row.
    """
    out: List[str] = []
    seen_urls: Set[str] = set()
    per_host: Dict[str, int] = {}
    for tmpl in settings.search_endpoints:
        if len(out) >= limit:
            break
        res = fetch(tmpl.format(q=quote_plus(query)), allow_browser=True)
        if not res.ok:
            continue
        hrefs = _DDG_LINK.findall(res.html) or _DDG_LITE.findall(res.html) or _ANY_LINK.findall(res.html)
        for href in hrefs:
            url = _unwrap(href).split("#")[0]
            if not url.startswith("http") or url in seen_urls:
                continue
            h = host_of(url)
            if not h or is_banned(url):
                continue
            if any(x in h for x in ("duckduckgo.com", "marcia.cc")):
                continue
            # Several results per host: the spec sheet and the product page often
            # sit on the same domain, and host-level dedup would lose one of them.
            if per_host.get(h, 0) >= 3:
                continue
            per_host[h] = per_host.get(h, 0) + 1
            seen_urls.add(url)
            out.append(url)
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------------------
# Manufacturer domain
# ---------------------------------------------------------------------------
def _domain_affinity(url: str, names: Iterable[str]) -> float:
    h = host_of(url).split(":")[0]
    bare = re.sub(r"\.(com|net|org|co|io|us|ca|de|uk|eu|in|cn|au|biz|info)(\.[a-z]{2})?$", "", h)
    # Compare against the whole host and against its last label, so a brand's
    # shop or country subdomain (shop.kichler.com) still reads as its own site.
    labels = [p for p in bare.split(".") if p]
    stems = {bare.replace("-", "").replace(".", "")}
    if labels:
        stems.add(labels[-1].replace("-", ""))
    best = 0.0
    for n in names:
        for stem in stems:
            best = max(best, _stem_affinity(core_name(n).replace(" ", ""), stem))
    return best


def _stem_affinity(k: str, stem: str) -> float:
    """How strongly does a name `k` claim a domain stem?"""
    if not k or not stem:
        return 0.0
    if k == stem:
        return 1.0
    # A prefix match only means something for a distinctive name. Allowing it on
    # short tokens makes "stud" match studiodesign.com, and the brand of a
    # dimensional stud becomes whatever site happened to rank for it.
    if min(len(k), len(stem)) >= 6 and (stem.startswith(k) or k.startswith(stem)):
        return 1.0
    return fuzz.ratio(k, stem) / 100.0


_PATH_STOP = {
    "www", "com", "net", "org", "en", "us", "ca", "uk", "au", "html", "htm", "php", "aspx",
    "cgi", "bin", "product", "products", "item", "items", "p", "pd", "sku", "shop", "store",
    "catalog", "category", "manual", "manuals", "review", "reviews", "detail", "details",
    "spec", "specs", "buy", "sale", "price", "new", "the", "and", "for", "with", "inch",
    "built", "in", "series", "model", "part", "parts", "home", "s", "dp", "ref", "search",
    "collections", "pages", "content", "media", "images", "assets", "appliance", "appliances",
}


def infer_brand_tokens(mpn: str, extra_context: str = "", limit: int = 3,
                       prefer: str = "") -> List[str]:
    """Derive the brand from the open web without a brand master file.

    A part number is close to unique, and third parties habitually write it as
    "<brand>-<part number>" in their URLs and titles. So the token that keeps
    appearing next to the part number across *independent hosts* is the brand -
    a crowd-sourced, evidence-backed answer that needs no dictionary and no LLM.
    """
    variants = mpn_variants(mpn)[:2]
    if not variants:
        return []
    # Words the distributor already used are product nouns, not brands. Dropping
    # them is what separates "frigidaire" from "dishwasher" and "professional",
    # and stops dishwashermanuals.org from passing as the manufacturer's site.
    described = {t for t in norm(extra_context).split() if len(t) > 2}
    # Path votes and host votes are counted separately and weighted differently.
    # A real brand shows up inside *other people's* URLs ("/whirlpool-wdts7024rz"),
    # whereas a retailer's own name only ever appears in its own hostname. Giving
    # host tokens a quarter vote stops "ktappliance" out-ranking the brand simply
    # because ktappliance.com happened to rank for the part number.
    path_votes: Dict[str, Set[str]] = {}
    host_votes: Dict[str, Set[str]] = {}

    def _tokens_of(text: str, vl: str):
        for token in re.split(r"[^a-z0-9]+", text.lower()):
            if (len(token) < 3 or len(token) > 24 or token in _PATH_STOP
                    or token in described
                    or token.isdigit() or vl in token or token in vl):
                continue
            if token.isalpha():
                yield token

    for v in variants:
        vl = v.lower()
        for url in web_search('"{}" {}'.format(v, clean(extra_context)[:40]).strip(), limit=14):
            host = host_of(url)
            for token in _tokens_of(urlparse(url).path, vl):
                path_votes.setdefault(token, set()).add(host)
            for token in _tokens_of(host, vl):
                host_votes.setdefault(token, set()).add(host)

    votes = {t: hosts for t, hosts in path_votes.items()}
    for t, hosts in host_votes.items():
        votes.setdefault(t, set())
    # Host count alone is not enough. Part numbers collide with other
    # vocabularies - "42275BK" reads as a hex colour, so colour-reference sites
    # rank for it and "color" out-votes the real brand. When the caller supplied
    # a manufacturer name, a token that corroborates it wins outright: two
    # independent signals agreeing beats one signal counted often.
    # Split on every non-alphanumeric: `norm` keeps "/", and a supplier string
    # like "Black & Decker/dewlt" would otherwise yield one token "decker/dewlt"
    # that matches nothing.
    hint_tokens = {t for t in re.split(r"[^a-z0-9]+", norm(core_name(prefer)))
                   if len(t) > 2}
    scored: List[Tuple[float, str]] = []
    for token in votes:
        score = len(path_votes.get(token, ())) + 0.25 * len(host_votes.get(token, ()))
        if hint_tokens:
            best = max((fuzz.ratio(token, h) for h in hint_tokens), default=0)
            if token in hint_tokens or best >= 85:
                score += 100.0                 # corroborated by the supplied name
        scored.append((score, token))
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    return [t for s, t in scored if s >= 2.0][:limit]


SUPPLIER_ACCOUNT = re.compile(
    r"\b(cooperative|co-?op|supply|supplies|distribut\w*|dealers?|wholesal\w*|"
    r"trading|industrial supply|parts? (inc|co|llc)|hardware)\b", re.I)


def looks_like_supplier_account(name: str) -> bool:
    """"Jam Industrial Supply LLC (JAMIN)" is a distributor account, not a maker."""
    return bool(SUPPLIER_ACCOUNT.search(clean(name)))


def find_manufacturer_domain(names: List[str], mpn: str = "", context: str = "",
                             prefer: str = "") -> Tuple[Optional[str], str]:
    """Resolve the official domain from the open web. Returns (domain, evidence-note).

    The supplier string on an input row is usually a *distributor account*
    ("Appliance Dealers Cooperative (APPDE)"), not the manufacturer, so the part
    number leads. A part number is close to globally unique: search it, discard
    marketplaces, distributors and generic sites, and the domain that keeps
    coming back - especially with the part number in its own URL path - is the
    manufacturer.
    """
    names = [clean(n) for n in names if clean(n) and not norm(n).startswith("no ")]
    scored: Dict[str, Tuple[float, str]] = {}

    # --- brand inferred from the crowd, then matched to a domain -----------
    inferred = infer_brand_tokens(mpn, context, prefer=prefer) if mpn else []
    if inferred:
        names = list(dict.fromkeys(inferred + names))

    # --- part-number-led discovery (works with no usable manufacturer name) --
    if mpn:
        frequency: Dict[str, float] = {}
        notes: Dict[str, str] = {}
        for v in mpn_variants(mpn)[:2]:
            for q in ('"{}"'.format(v), '"{}" {}'.format(v, clean(context)[:60]).strip()):
                for rank, url in enumerate(web_search(q, limit=12)):
                    if is_generic(url) or is_distributor(url) or is_banned(url):
                        continue
                    h = host_of(url)
                    weight = 1.0 + max(0.0, (10 - rank) / 10.0)
                    if v.lower() in urlparse(url).path.lower():
                        weight += 2.0          # the part number is in their own URL
                    if inferred and _domain_affinity(url, inferred) > 0.85:
                        weight += 4.0          # the domain IS the inferred brand
                    elif names and _domain_affinity(url, names) > 0.75:
                        weight += 2.0          # or matches a supplied name
                    frequency[h] = frequency.get(h, 0.0) + weight
                    notes.setdefault(h, "part-number search: {}".format(q))
        for h, w in frequency.items():
            if w >= 2.0:
                scored[h] = (min(0.99, 0.5 + w / 12.0), notes[h])

    if not names and not scored:
        return None, "no manufacturer name and no part-number hits"

    primary = names[0] if names else ""
    queries = []
    if primary:
        queries = ["{} official site".format(primary),
                   "{} manufacturer official website".format(primary)]
    for q in queries:
        for url in web_search(q, limit=10):
            if is_generic(url) or is_distributor(url):
                continue
            aff = _domain_affinity(url, names)
            if aff <= 0.55:
                continue
            h = host_of(url)
            prev = scored.get(h, (0.0, ""))
            if aff > prev[0]:
                scored[h] = (aff, "search: {}".format(q))

    # Direct-guess tier: cheap, and validated against the site's own <title>.
    # Supplier *accounts* (co-ops, industrial supply houses) are not manufacturers,
    # so their names never seed a domain guess.
    guessable = [n for n in names
                 if not re.search(r"\b(cooperative|co-?op|supply|supplies|distribut\w*|"
                                  r"dealers?|wholesal\w*|industrial supply|trading)\b", n, re.I)]
    for n in guessable[:2]:
        slug = core_name(n).replace(" ", "")
        if not (3 <= len(slug) <= 30):
            continue
        for tld in (".com", ".net"):
            cand = "https://www." + slug + tld
            if host_of(cand) in scored:
                continue
            r = fetch(cand, allow_browser=False)
            if not r.ok:
                continue
            m = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.html)
            title = clean(re.sub(r"(?s)<[^>]+>", " ", m.group(1))) if m else ""
            if title and fuzz.partial_ratio(core_name(n), norm(title)) >= 85:
                scored[host_of(r.final_url)] = (0.95, "direct-guess validated by <title>: " + title[:80])

    if not scored:
        return None, "no official domain resolved for " + (primary or mpn)

    # A domain whose registrable name IS the inferred brand outranks anything
    # that merely appeared often. Retailers rank well for a part number; only the
    # manufacturer is named after the brand.
    owner_names = [n for n in (inferred[:1] + ([clean(prefer)] if clean(prefer) else []))
                   if n and not looks_like_supplier_account(n)]
    if owner_names:
        owned = {h: v for h, v in scored.items()
                 if _domain_affinity("https://" + h, owner_names) >= 0.9}
        if owned:
            best = max(owned.items(), key=lambda kv: kv[1][0])
            return best[0], "domain matches brand '{}' ({})".format(
                owner_names[0], best[1][1])

    best = max(scored.items(), key=lambda kv: kv[1][0])
    return best[0], best[1][1]


# ---------------------------------------------------------------------------
# Product page
# ---------------------------------------------------------------------------
_MPN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def mpn_variants(mpn: str) -> List[str]:
    """Distributor part numbers carry vendor prefixes ('3MABR-7100075678')."""
    m = clean(mpn)
    out = [m]
    if "-" in m:
        head, _, tail = m.partition("-")
        if tail and (len(head) <= 6 or head.isalpha()):
            out.append(tail)
        out.append(m.replace("-", ""))
    if m and m[-1].isalpha() and len(m) > 5:
        out.append(m[:-1])
    seen, uniq = set(), []
    for v in out:
        if v and v.upper() not in seen:
            seen.add(v.upper())
            uniq.append(v)
    return uniq


def onsite_search_urls(domain: str, term: str) -> List[str]:
    """Common on-site search routes - cheaper and more precise than a SERP."""
    t = quote_plus(term)
    base = "https://" + domain
    return [
        "{}/search?q={}".format(base, t),
        "{}/search?searchTerm={}".format(base, t),
        "{}/catalogsearch/result/?q={}".format(base, t),
        "{}/s?q={}".format(base, t),
        "{}/en/search?q={}".format(base, t),
        "{}/us/en/search?q={}".format(base, t),
    ]


def domain_matches(url: str, names: Iterable[str], threshold: float = 0.9) -> bool:
    """Is this URL served by a site named after one of `names`?"""
    return bool(url) and _domain_affinity(url, [n for n in names if n]) >= threshold


def same_brand_site(url: str, domain: Optional[str], names: Iterable[str]) -> bool:
    """whirlpool.ca, shop.whirlpool.com and whirlpool.com are all "their own site".

    A manufacturer's product may live on a country or shop sub-brand of the same
    registrable name, so tier 1 is decided by the brand stem, not an exact host.
    """
    if domain and host_of(url) == domain:
        return True
    return _domain_affinity(url, names) >= 0.95


def _product_url_score(url: str, mpn: str, domain: Optional[str]) -> float:
    u = url.lower()
    path = urlparse(u).path
    s = 0.0
    for v in mpn_variants(mpn):
        vl = v.lower()
        if vl and vl in path:
            s += 3.0
            break
        if vl and vl in u:
            s += 1.5
            break
    for kw, w in (("/product", 1.2), ("/products/", 1.2), ("/p/", 1.0), ("/item", .8),
                  ("/sku", .8), ("catalog", .5), ("spec", .4), ("/model", .6)):
        if kw in u:
            s += w
    for kw in ("/search", "?q=", "/blog", "/news", "/press", "/careers", "/contact",
               "/cart", "/login", "/legal", "/privacy", "sitemap"):
        if kw in u:
            s -= 1.5
    if domain and host_of(url) == domain:
        s += 2.0
    if path.count("/") >= 2:
        s += 0.3
    return s


def find_product_pages(mpn: str, names: List[str], domain: Optional[str],
                       limit: int = None) -> List[SourceCandidate]:
    limit = limit or settings.max_pages_per_item
    cands: Dict[str, SourceCandidate] = {}

    def add(url: str, tier: int, reason: str):
        url = url.split("#")[0]
        if not url.startswith("http") or is_banned(url) or is_generic(url):
            return
        if url in cands:
            return
        cands[url] = SourceCandidate(url=url, tier=tier, reason=reason,
                                     score=_product_url_score(url, mpn, domain))

    variants = mpn_variants(mpn)
    primary = names[0] if names else ""

    # --- tier 1: the manufacturer's own domain ---------------------------
    if domain:
        for v in variants[:2]:
            for u in web_search('site:{} "{}"'.format(domain, v), limit=8):
                if same_brand_site(u, domain, names):
                    add(u, 1, "site-restricted search for " + v)

    # --- tier 1b: open web, kept when it lands on any site of the same brand --
    for v in variants[:2]:
        for q in ('"{}" {}'.format(v, primary).strip(), '"{}"'.format(v)):
            for u in web_search(q, limit=12):
                if same_brand_site(u, domain, names):
                    add(u, 1, "web search -> manufacturer site")

    # --- tier 2: reputed distributors, fallback only ---------------------
    if not any(c.tier == 1 and c.score > 2.0 for c in cands.values()):
        for v in variants[:2]:
            for u in web_search('"{}" {} specifications'.format(v, primary), limit=10):
                if is_distributor(u):
                    add(u, 2, "distributor fallback (no manufacturer page found)")

    ranked = sorted(cands.values(), key=lambda c: (c.tier, -c.score))
    return ranked[:limit]


_DOC_QUERIES = (
    '"{mpn}" {brand} specification sheet pdf',
    '"{mpn}" {brand} product specifications pdf',
    '"{mpn}" {brand} installation instructions pdf',
    '"{mpn}" {brand} owners manual pdf',
    '"{mpn}" {brand} dimension guide pdf',
)


def find_documents(mpn: str, names: List[str], domain: Optional[str],
                   limit: int = 6) -> List[SourceCandidate]:
    """Locate the manufacturer's own PDFs for a part.

    Manufacturer product pages increasingly render their spec panel from a
    client-side service call, so the numbers a distributor actually needs -
    voltage, amperage, dimensions - are not in the HTML at all. They are in the
    spec sheet PDF, which is indexed and is unambiguously the manufacturer's own
    document. Searching for it directly is both cheaper and more reliable than
    driving the page's accordions.
    """
    brand = clean(names[0]) if names else ""
    out: Dict[str, SourceCandidate] = {}
    for v in mpn_variants(mpn)[:2]:
        for tmpl in _DOC_QUERIES:
            q = tmpl.format(mpn=v, brand=brand).strip()
            for u in web_search(q, limit=8):
                low = u.lower().split("?")[0]
                if not low.endswith(".pdf") or is_banned(u):
                    continue
                if not same_brand_site(u, domain, names):
                    continue
                out.setdefault(u, SourceCandidate(url=u, tier=1, reason="manufacturer PDF: " + q,
                                                  score=3.0 if v.lower() in low else 1.0))
        if len(out) >= limit:
            break
    return sorted(out.values(), key=lambda c: -c.score)[:limit]


def extract_links(base_url: str, html: str) -> List[str]:
    out, seen = [], set()
    for m in re.finditer(r'(?is)<a[^>]+href=["\']([^"\']+)["\']', html):
        u = urljoin(base_url, htmllib.unescape(m.group(1))).split("#")[0]
        if u.startswith("http") and u not in seen:
            seen.add(u)
            out.append(u)
    return out
