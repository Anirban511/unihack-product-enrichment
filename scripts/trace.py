"""Trace exactly what the scraper searched, fetched, and rejected for one part.

    python -m scripts.trace 3MABR-7100075678 "Jam Industrial Supply LLC (JAMIN)"

Prints every search query with its results, every URL fetched with its outcome,
and the reason each candidate page was accepted or thrown away - so a failure
can be attributed to the query, the ranking, the fetch, or the acceptance gate
rather than guessed at.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.acquire import discovery, fetcher
from app.evidence import EvidenceStore
from app.extract.parse import merge_facts
from app.pipeline import EnrichmentInput, acquire, analyse_input

SEARCHES: list = []
FETCHES: list = []


def instrument() -> None:
    real_search = discovery.web_search
    real_fetch = fetcher.fetch

    def traced_search(query: str, limit: int = 12):
        out = real_search(query, limit)
        SEARCHES.append((query, out))
        return out

    def traced_fetch(url: str, allow_browser: bool = True, use_cache: bool = True):
        res = real_fetch(url, allow_browser=allow_browser, use_cache=use_cache)
        if "duckduckgo" not in url:
            FETCHES.append((url, res.status, res.tier, len(res.html or ""),
                            res.error[:60]))
        return res

    discovery.web_search = traced_search
    discovery.fetch = traced_fetch
    fetcher.fetch = traced_fetch
    # modules that imported `fetch` by value need rebinding too
    import app.pipeline as P
    import app.acquire.pdfs as D
    P.fetch = traced_fetch
    D.fetch = traced_fetch


def main() -> int:
    part = sys.argv[1] if len(sys.argv) > 1 else "3MABR-7100075678"
    maker = sys.argv[2] if len(sys.argv) > 2 else ""
    instrument()

    item = EnrichmentInput(Mfg_Part_Num=part, Part_Manuf=maker)
    analysis = analyse_input(item)

    print("=" * 78)
    print("PART            :", part)
    print("SUPPLIER STRING :", maker or "(none)")
    print("PART VARIANTS   :", analysis["mpn_variants"])
    print("=" * 78)

    store = EvidenceStore()
    warnings: list = []
    pages, sources, domain = acquire(analysis, store, warnings)

    print("\nINFERRED BRAND TOKENS :", analysis.get("inferred_brand_tokens"))
    print("RESOLVED DOMAIN       :", domain)

    print("\n" + "-" * 78)
    print("SEARCH QUERIES ({})".format(len(SEARCHES)))
    print("-" * 78)
    for q, results in SEARCHES:
        print("\n  QUERY: {}".format(q))
        if not results:
            print("     (no results)")
        for u in results[:6]:
            print("     - {}".format(u[:110]))

    print("\n" + "-" * 78)
    print("PAGES FETCHED ({})".format(len(FETCHES)))
    print("-" * 78)
    for url, status, tier, size, err in FETCHES:
        print("  {:>4} {:<14} {:>7}b  {}".format(status, tier, size, url[:88]))
        if err:
            print("       error: {}".format(err))

    print("\n" + "-" * 78)
    print("CANDIDATE OUTCOMES ({})".format(len(sources)))
    print("-" * 78)
    for s in sources:
        verdict = s.get("skipped") or s.get("error") or "ACCEPTED"
        print("  tier{} {:<9} {}".format(s.get("tier"), str(s.get("status")),
                                         (s.get("final_url") or s.get("url", ""))[:82]))
        print("        why chosen : {}".format(s.get("reason", "")[:70]))
        print("        outcome    : {}".format(verdict[:70]))

    facts = merge_facts(pages) if pages else None
    print("\n" + "-" * 78)
    print("RESULT")
    print("-" * 78)
    print("  pages accepted :", len(pages))
    print("  evidence units :", store.stats()["evidence_units"])
    if facts:
        print("  page used      :", facts.url[:88])
        print("  title          :", facts.title[:88])
        print("  spec pairs     :", len(facts.specs))
    for w in warnings:
        print("  warning        :", w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
