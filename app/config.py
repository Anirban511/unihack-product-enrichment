"""Central configuration. Nothing about products is configured here - only infrastructure."""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM -------------------------------------------------------------
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: str = "low"      # cost control: low unless a task needs more
    groq_max_tokens: int = 8000
    groq_temperature: float = 0.0           # determinism: never sample
    llm_max_calls_per_item: int = 6         # hard budget; pipeline degrades gracefully past it

    # --- Paths -----------------------------------------------------------
    reference_dir: Path = ROOT / "data" / "reference"
    cache_dir: Path = ROOT / "data" / "cache"
    delivery_format_csv: Path = ROOT / "Unihack_ Expected Output - Delivery Format.csv"
    sample_input_csv: Path = ROOT / "Unihack_ Sample Dataset - Input(1).csv"

    # --- Acquisition -----------------------------------------------------
    http_timeout: float = 25.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    enable_selenium: bool = True            # JS-rendering fallback tier
    selenium_page_timeout: int = 30
    # One browser serialises every JS-rendered fetch in the process, which is the
    # dominant cost at catalogue scale. Each instance costs ~250 MB.
    browser_pool_size: int = 3
    # Containers ship their own Chromium and driver; Selenium Manager cannot
    # download one when the image has no outbound access at build time, so the
    # paths are configurable and left empty for local runs (auto-detect).
    chrome_binary: str = ""                 # e.g. /usr/bin/chromium
    chromedriver_path: str = ""             # e.g. /usr/bin/chromedriver
    max_pages_per_item: int = 6
    max_pdfs_per_item: int = 3
    cache_ttl_seconds: int = 60 * 60 * 24 * 7

    # --- Grounding -------------------------------------------------------
    # A value must be verbatim-recoverable from cited evidence at >= this ratio,
    # otherwise it is discarded. 1.0 == exact normalised containment.
    grounding_min_ratio: float = 0.92
    review_confidence_threshold: float = 0.75

    # --- Sourcing policy (guide s.4 "Sourcing rules apply") --------------
    # Consumer marketplaces are *never* acceptable, at any tier.
    banned_domains: List[str] = [
        "amazon.", "ebay.", "walmart.", "alibaba.", "aliexpress.", "etsy.",
        "wish.com", "temu.com", "target.com", "costco.com", "homedepot.com",
        "lowes.com", "wayfair.com", "bestbuy.com", "newegg.", "rakuten.",
        "flipkart.", "shopee.", "mercadolibre.", "overstock.com", "sears.com",
        "menards.com", "acehardware.com", "samsclub.com", "bjs.com",
        "pinterest.", "facebook.", "instagram.", "reddit.", "quora.",
        "alibaba.com", "made-in-china.com", "indiamart.com", "tradeindia.",
    ]
    # Tier-2 fallback only (guide: "reputed third-party distributors").
    distributor_domains: List[str] = [
        "grainger.com", "mcmaster.com", "fastenal.com", "motion.com",
        "zoro.com", "mscdirect.com", "globalindustrial.com", "rexelusa.com",
        "cedgraybar.com", "graybar.com", "wesco.com", "ferguson.com",
        "supplyhouse.com", "platt.com", "statesupply.com", "digikey.com",
        "mouser.com", "newark.com", "alliedelec.com", "rsdelivers.com",
        "acmetools.com", "toolup.com", "ohiopowertool.com",
    ]
    # Search backends that do not require an API key. Ordered cheapest-first:
    # the html endpoint returns a ~40 KB SERP vs ~270 KB for the JS front page.
    search_endpoints: List[str] = [
        "https://html.duckduckgo.com/html/?q={q}",
        "https://lite.duckduckgo.com/lite/?q={q}",
    ]


settings = Settings()
settings.cache_dir.mkdir(parents=True, exist_ok=True)
settings.reference_dir.mkdir(parents=True, exist_ok=True)
