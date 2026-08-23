"""Parallel, resumable catalogue run.

Enrichment is almost entirely network wait - SERPs, product pages, PDFs - so the
work parallelises well across threads even in one process. The shared disk cache
and the browser pool are both thread-safe, and a row that has already been
written is never repeated, so a run can be stopped and restarted at will.

    python -m scripts.bulk --limit 50 --workers 6
    python -m scripts.bulk                       # the whole file
    python -m scripts.bulk --resume              # continue where it stopped

Two files are written, and both are updated after every row:

    out/bulk_delivery.csv   the 252-column delivery format
    out/bulk_report.csv     one status line per part: OK / REVIEW / NOT FOUND
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.acquire import fetcher
from app.config import settings
from app.delivery import columns
from app.pipeline import EnrichmentInput, enrich
from app.textnorm import clean

REPORT_COLUMNS = ["Part number", "Manufacturer in", "Status", "Confidence",
                  "Source tier", "Source URL", "Brand", "Product name",
                  "Classpath", "Attributes", "Citations", "Seconds", "Warnings"]

_write_lock = threading.Lock()


def row_status(result) -> str:
    tier = result.metrics.get("source_tier")
    if not tier:
        return "NOT FOUND"
    if tier == 2:
        return "REVIEW - third-party source"
    if result.needs_human_review:
        return "REVIEW - incomplete"
    return "OK"


def already_done(path: Path, key_column: str) -> Set[str]:
    if not path.exists():
        return set()
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            return {clean(r.get(key_column, "")) for r in csv.DictReader(fh)}
    except Exception:
        return set()


def open_appending(path: Path, header: List[str], resume: bool):
    """Open for append, writing the header only when starting a fresh file."""
    fresh = not (resume and path.exists() and path.stat().st_size > 0)
    fh = path.open("w" if fresh else "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore",
                            lineterminator="\n")
    if fresh:
        writer.writeheader()
        fh.flush()
    return fh, writer


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(settings.sample_input_csv))
    ap.add_argument("--out", default="out")
    ap.add_argument("--limit", type=int, default=0, help="0 = every row")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--browsers", type=int, default=0,
                    help="Chrome instances; defaults to workers, capped at 6")
    ap.add_argument("--resume", action="store_true",
                    help="skip parts already present in the output")
    args = ap.parse_args()

    settings.browser_pool_size = args.browsers or min(max(args.workers, 1), 6)

    with open(args.input, encoding="utf-8-sig", newline="") as fh:
        catalogue = list(csv.DictReader(fh))
    window = catalogue[args.offset:]
    if args.limit:
        window = window[: args.limit]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    delivery_path = out_dir / "bulk_delivery.csv"
    report_path = out_dir / "bulk_report.csv"

    done = already_done(report_path, "Part number") if args.resume else set()
    todo = [r for r in window if clean(r.get("Mfg_Part_Num", "")) not in done]

    print("catalogue rows : {}".format(len(window)))
    print("already done   : {}".format(len(window) - len(todo)))
    print("to process     : {}".format(len(todo)))
    print("workers        : {}  (browser pool {})".format(args.workers,
                                                          settings.browser_pool_size))
    print("-" * 68, flush=True)
    if not todo:
        print("nothing to do")
        return 0

    d_fh, d_writer = open_appending(delivery_path, columns(), args.resume)
    r_fh, r_writer = open_appending(report_path, REPORT_COLUMNS, args.resume)

    counts = {"OK": 0, "REVIEW": 0, "NOT FOUND": 0, "ERROR": 0}
    started = time.time()

    def work(src: Dict[str, str]) -> Optional[dict]:
        part = clean(src.get("Mfg_Part_Num", ""))
        if not part:
            return None
        t0 = time.time()
        item = EnrichmentInput(
            Mfg_Part_Num=part,
            Part_Manuf=clean(src.get("Part_Manuf", "")),
            Part_Desc=clean(src.get("Part_Desc", "")),
            E1_Brand=clean(src.get("E1_Brand", "")),
            Unilog_Brand=clean(src.get("Unilog_Brand", "")),
            DIB_Brand=clean(src.get("DIB_Brand", "")),
        )
        try:
            result = enrich(item)
        except Exception as exc:
            return {"row": None, "report": {
                "Part number": part, "Manufacturer in": item.Part_Manuf,
                "Status": "ERROR", "Confidence": 0.0, "Source tier": "",
                "Source URL": "", "Brand": "", "Product name": "", "Classpath": "",
                "Attributes": 0, "Citations": 0,
                "Seconds": round(time.time() - t0, 1),
                "Warnings": "{}: {}".format(type(exc).__name__, str(exc)[:180])}}

        row = result.delivery_row
        attrs = sum(1 for i in range(1, 51)
                    if clean(row.get("ATTRIBUTE_VALUE {}".format(i), "")))
        return {"row": row, "report": {
            "Part number": part, "Manufacturer in": item.Part_Manuf,
            "Status": row_status(result), "Confidence": round(result.confidence, 2),
            "Source tier": result.metrics.get("source_tier") or "",
            "Source URL": clean(row.get("MFR URL", "")),
            "Brand": clean(row.get("BRAND_NAME", "")),
            "Product name": clean(row.get("Product Name", "")),
            "Classpath": clean(row.get("Classpath", "")),
            "Attributes": attrs, "Citations": len(result.provenance),
            "Seconds": round(time.time() - t0, 1),
            "Warnings": " | ".join(result.warnings)[:300]}}

    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(work, src): src for src in todo}
            for fut in as_completed(futures):
                out = fut.result()
                if out is None:
                    continue
                completed += 1
                rep = out["report"]
                bucket = ("ERROR" if rep["Status"] == "ERROR"
                          else "NOT FOUND" if rep["Status"] == "NOT FOUND"
                          else "OK" if rep["Status"] == "OK" else "REVIEW")
                counts[bucket] += 1

                # Flush after every row: a long run must survive being killed.
                with _write_lock:
                    if out["row"]:
                        d_writer.writerow(out["row"])
                        d_fh.flush()
                    r_writer.writerow(rep)
                    r_fh.flush()

                rate = completed / max(1e-9, (time.time() - started)) * 60
                remaining = (len(todo) - completed) / max(1e-9, rate)
                print("[{:>4}/{}] {:<22} {:<28} attrs={:<3} {:>5.0f}s   "
                      "| {:.1f}/min, ~{:.0f} min left"
                      .format(completed, len(todo), rep["Part number"][:22],
                              rep["Status"][:28], rep["Attributes"], rep["Seconds"],
                              rate, remaining), flush=True)
    except KeyboardInterrupt:
        print("\ninterrupted - progress is saved, re-run with --resume", flush=True)
    finally:
        d_fh.close()
        r_fh.close()
        fetcher.shutdown()

    elapsed = time.time() - started
    print("-" * 68)
    print("processed {} rows in {:.1f} min ({:.1f} rows/min)".format(
        completed, elapsed / 60, completed / max(1e-9, elapsed / 60)))
    for k in ("OK", "REVIEW", "NOT FOUND", "ERROR"):
        pct = 100.0 * counts[k] / max(1, completed)
        print("  {:<10} {:>4}   {:>5.1f}%".format(k, counts[k], pct))
    print("\n{}\n{}".format(delivery_path, report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
