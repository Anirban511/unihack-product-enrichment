"""Two-tier page acquisition: cheap HTTP first, real browser only when needed.

Manufacturer product pages are the worst case for scraping - most of the big
brands ship a JS shell and hydrate the spec table client-side, so a plain GET
returns navigation and nothing else. But launching Chrome for every URL is slow
and expensive at 1,000-row scale.

So: fetch over HTTP, run a *shell detector* over the result, and escalate to a
headless browser only for the pages that genuinely need it. Everything is
disk-cached, so a re-run of the same catalogue costs no network at all.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx
from diskcache import Cache

from app.config import settings

_cache = Cache(str(settings.cache_dir / "http"), size_limit=2 * 1024 ** 3)

# Markers that say "the real content arrives via JS".
_SHELL_MARKERS = (
    "__next_data__", "window.__nuxt__", "ng-version", "data-reactroot",
    "id=\"__nuxt\"", "id=\"root\"></div>", "id=\"app\"></div>",
    "please enable javascript", "you need to enable javascript",
)


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    html: str = ""
    body: bytes = b""
    tier: str = ""           # http | browser | cache | failed
    error: str = ""
    elapsed_ms: int = 0
    meta: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == 200 and (bool(self.html) or bool(self.body))

    @property
    def is_pdf(self) -> bool:
        return "pdf" in self.content_type.lower() or self.final_url.lower().split("?")[0].endswith(".pdf")


def _key(url: str, tier: str) -> str:
    return "{}:{}".format(tier, hashlib.sha256(url.encode("utf-8")).hexdigest())


def _visible_text_len(html: str) -> int:
    stripped = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    stripped = re.sub(r"(?s)<[^>]+>", " ", stripped)
    return len(re.sub(r"\s+", " ", stripped).strip())


# Markup that advertises a spec panel the server did not render.
_DEFERRED_SPECS = re.compile(
    r'(data-service-url="?specs|pdp-accordion|data-accordion|role="tab"|'
    r'accordion__|tab-pane|js-specs|data-tab|aria-controls="[^"]*spec)', re.I)


def looks_like_js_shell(html: str) -> bool:
    if not html:
        return True
    low = html.lower()
    if _visible_text_len(html) < 1200:
        return True
    return any(m in low for m in _SHELL_MARKERS) and _visible_text_len(html) < 4000


def specs_look_deferred(html: str) -> bool:
    """The page has plenty of prose but almost no label/value rows, and its own
    markup says a specifications panel exists. That panel is hydrated by JS, so
    the cheap HTTP tier silently returns a product page with no product data -
    the single most common failure mode in manufacturer scraping."""
    if not html:
        return False
    rows = len(re.findall(r"(?i)<tr\b", html)) + len(re.findall(r"(?i)<dt\b", html))
    return rows < 8 and bool(_DEFERRED_SPECS.search(html))


# ---------------------------------------------------------------------------
# Tier 1 - HTTP
# ---------------------------------------------------------------------------
def _headers(url: str) -> Dict[str, str]:
    host = urlparse(url).netloc
    return {
        "User-Agent": settings.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://{}/".format(host) if host else "",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


def _http_fetch(url: str) -> FetchResult:
    t0 = time.time()
    res = FetchResult(url=url, tier="http")
    try:
        with httpx.Client(follow_redirects=True, timeout=settings.http_timeout,
                          headers=_headers(url), verify=False) as client:
            r = client.get(url)
            res.status = r.status_code
            res.final_url = str(r.url)
            res.content_type = r.headers.get("content-type", "")
            if res.is_pdf:
                res.body = r.content
            else:
                res.html = r.text
    except Exception as exc:
        res.tier, res.error = "failed", "{}: {}".format(type(exc).__name__, exc)
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


# ---------------------------------------------------------------------------
# Tier 2 - headless browser (Selenium). One driver, reused, lazily started.
# ---------------------------------------------------------------------------
# Where Debian/Ubuntu images put Chromium once `packages.txt` has installed it.
# Probing beats configuration: the same code then runs unchanged on a laptop, a
# Docker Space and a Gradio Space, none of which agree on the path.
_CHROME_CANDIDATES = ("/usr/bin/chromium", "/usr/bin/chromium-browser",
                      "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable")
_DRIVER_CANDIDATES = ("/usr/bin/chromedriver", "/usr/lib/chromium/chromedriver",
                      "/usr/lib/chromium-browser/chromedriver",
                      "/usr/local/bin/chromedriver")


def _first_existing(configured: str, candidates) -> str:
    import os
    if configured and os.path.exists(configured):
        return configured
    for path in candidates:
        if os.path.exists(path):
            return path
    return ""


class _Browser:
    _lock = threading.Lock()
    _driver = None
    _dead = False

    @classmethod
    def driver(cls):
        if cls._dead or not settings.enable_selenium:
            return None
        with cls._lock:
            if cls._driver is not None:
                return cls._driver
            try:
                from selenium import webdriver
                from selenium.webdriver.chrome.options import Options
                opts = Options()
                for flag in ("--headless=new", "--disable-gpu", "--no-sandbox",
                             "--disable-dev-shm-usage", "--window-size=1440,2400",
                             "--blink-settings=imagesEnabled=false",
                             "--disable-blink-features=AutomationControlled",
                             "--log-level=3"):
                    opts.add_argument(flag)
                opts.add_argument("--user-agent=" + settings.user_agent)
                opts.set_capability("pageLoadStrategy", "eager")
                opts.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
                binary = _first_existing(settings.chrome_binary, _CHROME_CANDIDATES)
                driver_path = _first_existing(settings.chromedriver_path, _DRIVER_CANDIDATES)
                if binary:
                    opts.binary_location = binary
                if driver_path:
                    from selenium.webdriver.chrome.service import Service
                    d = webdriver.Chrome(options=opts, service=Service(driver_path))
                else:
                    # Local dev: let Selenium Manager find the installed Chrome.
                    d = webdriver.Chrome(options=opts)
                d.set_page_load_timeout(settings.selenium_page_timeout)
                cls._driver = d
                return d
            except Exception:
                cls._dead = True     # no Chrome on this box - degrade, never crash
                return None

    @classmethod
    def quit(cls):
        with cls._lock:
            if cls._driver is not None:
                try:
                    cls._driver.quit()
                except Exception:
                    pass
                cls._driver = None


def _browser_fetch(url: str) -> FetchResult:
    t0 = time.time()
    res = FetchResult(url=url, tier="browser")
    d = _Browser.driver()
    if d is None:
        res.tier, res.error = "failed", "browser tier unavailable"
        return res
    try:
        with _Browser._lock:
            d.get(url)
            # Hydration + lazy spec tabs: settle on DOM size rather than a fixed sleep.
            last, stable = -1, 0
            for _ in range(20):
                time.sleep(0.35)
                size = d.execute_script("return document.documentElement.innerHTML.length")
                stable = stable + 1 if size == last else 0
                last = size
                if stable >= 2:
                    break
            # Open collapsed spec/feature accordions - specs are usually behind them.
            try:
                d.execute_script(
                    "document.querySelectorAll('details').forEach(e=>e.open=true);"
                    "var sel='[aria-expanded=\"false\"],[data-accordion],[data-service-url],"
                    ".accordion-toggle,.accordion__button,.tab,[role=\"tab\"],.pdp-sn-link,"
                    "[data-tab],[data-toggle=\"collapse\"]';"
                    "document.querySelectorAll(sel).forEach(function(e){try{e.click()}catch(_){}});"
                    "Array.from(document.querySelectorAll('a,button')).filter(function(e){"
                    "  return /spec|detail|dimension|feature|more/i.test(e.textContent||'')"
                    "      && (e.textContent||'').length < 40;"
                    "}).slice(0,12).forEach(function(e){try{e.click()}catch(_){}});"
                )
                # Panels hydrate from a service endpoint after the click, so
                # settle on DOM size again rather than guessing a sleep.
                last, stable = -1, 0
                for _ in range(14):
                    time.sleep(0.4)
                    size = d.execute_script("return document.documentElement.innerHTML.length")
                    stable = stable + 1 if size == last else 0
                    last = size
                    if stable >= 2:
                        break
            except Exception:
                pass
            res.html = d.execute_script("return document.documentElement.outerHTML")
            res.final_url = d.current_url
        res.status = 200 if res.html else 0
        res.content_type = "text/html"
    except Exception as exc:
        res.tier, res.error = "failed", "{}: {}".format(type(exc).__name__, exc)
    res.elapsed_ms = int((time.time() - t0) * 1000)
    return res


# ---------------------------------------------------------------------------
def fetch(url: str, allow_browser: bool = True, use_cache: bool = True) -> FetchResult:
    ck = _key(url, "v3")
    if use_cache:
        hit = _cache.get(ck)
        if isinstance(hit, FetchResult):
            hit.tier = hit.tier + "+cache"
            return hit

    res = _http_fetch(url)
    if allow_browser and settings.enable_selenium and not res.is_pdf:
        if not res.ok or looks_like_js_shell(res.html) or specs_look_deferred(res.html):
            better = _browser_fetch(url)
            gained_rows = (len(re.findall(r"(?i)<tr\b", better.html or ""))
                           > len(re.findall(r"(?i)<tr\b", res.html or "")))
            if better.ok and (_visible_text_len(better.html) > _visible_text_len(res.html)
                              or gained_rows):
                better.meta["escalated_from"] = "http:{}".format(res.status)
                res = better

    if res.ok and use_cache:
        _cache.set(ck, res, expire=settings.cache_ttl_seconds)
    return res


def shutdown() -> None:
    _Browser.quit()


def cache_stats() -> dict:
    return {"entries": len(_cache), "volume_bytes": _cache.volume(),
            "browser_available": settings.enable_selenium and not _Browser._dead}
