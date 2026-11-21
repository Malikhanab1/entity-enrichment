"""
B2B agency lead-enrichment and lead-scoring pipeline.

Reads `websites.xlsx`, crawls/enriches each unique domain, scores it as a
lead, and writes `agency_leads.xlsx` (all rows) plus `hot_leads.xlsx`
(LeadScore >= 60, sorted descending). Resumable via checkpoint.jsonl.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import time

import aiohttp
import pandas as pd
from playwright.async_api import async_playwright

from checkpoint import CheckpointManager
from config import CONFIG
from crawler import DomainThrottle, USER_AGENT
from domain_cache import DomainCache
from logging_setup import logger
from pipeline import EMPTY_RESULT_FIELDS, process_website


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="B2B agency lead-enrichment and lead-scoring pipeline."
    )
    parser.add_argument(
        "--cleanstart",
        action="store_true",
        help=(
            "Ignore and delete any existing checkpoint.jsonl and start the "
            "crawl from scratch instead of resuming."
        ),
    )
    parser.add_argument(
        "--input",
        default=None,
        help=f"Input Excel path (default: {CONFIG.input_excel})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Prefix for output files (e.g. my_crawl generates my_crawl.xlsx and my_crawl_hot_leads.xlsx)",
    )
    return parser.parse_args()


def format_eta(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m"
    return f"{minutes / 60:.1f}h"


async def run_crawl(urls: list[str], already_done: dict[int, dict]) -> dict[int, dict]:
    """Crawl all rows not already present in the checkpoint, saving progress as it goes."""
    checkpoint = CheckpointManager(CONFIG.checkpoint_file)
    domain_cache = DomainCache()
    throttle = DomainThrottle(CONFIG.per_domain_delay)

    http_semaphore = asyncio.Semaphore(CONFIG.http_concurrency)
    pw_semaphore = asyncio.Semaphore(CONFIG.browser_concurrency)
    connector = aiohttp.TCPConnector(limit=CONFIG.http_concurrency, ttl_dns_cache=300)

    results: dict[int, dict] = dict(already_done)

    # Pre-warm domain cache from already-completed rows so duplicate domains
    # among the *remaining* rows don't get re-crawled.
    for row in already_done.values():
        domain = row.get("Domain")
        if domain and not domain_cache.has(domain):
            domain_cache.set(domain, {k: row.get(k, "") for k in EMPTY_RESULT_FIELDS})

    pending_indices = [i for i in range(len(urls)) if i not in already_done]
    total = len(urls)

    stats = {
        "processed": len(already_done),
        "successful": sum(
            1 for r in already_done.values() if r.get("WebsiteAccessible")
        ),
        "failed": sum(
            1 for r in already_done.values() if not r.get("WebsiteAccessible")
        ),
        "emails_found": sum(1 for r in already_done.values() if r.get("PrimaryEmail")),
        "hot": sum(1 for r in already_done.values() if r.get("LeadTier") == "HOT"),
        "warm": sum(1 for r in already_done.values() if r.get("LeadTier") == "WARM"),
    }

    start_time = time.monotonic()

    if not pending_indices:
        logger.info(
            "All rows already processed according to checkpoint. Nothing to crawl."
        )
        return results

    async def bounded_process(idx: int):
        async with http_semaphore:
            raw_url = urls[idx]
            res = await process_website(
                idx, raw_url, session, context, pw_semaphore, throttle, domain_cache
            )
            return res

    async with async_playwright() as p:
        browser = None
        context = None
        try:
            browser = await asyncio.wait_for(
                p.chromium.launch(
                    headless=True,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                    ],
                ),
                timeout=60,
            )
            context = await browser.new_context(
                user_agent=USER_AGENT, viewport={"width": 1280, "height": 800}
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
        except Exception as launch_err:
            logger.error(
                f"Failed to launch Playwright browser: {launch_err}. "
                f"Continuing with aiohttp-only crawling (no JS-rendered fallback)."
            )

        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                tasks = [
                    asyncio.create_task(bounded_process(idx)) for idx in pending_indices
                ]

                try:
                    for completed_count, task in enumerate(
                        asyncio.as_completed(tasks), 1
                    ):
                        try:
                            row_result = await task
                        except asyncio.CancelledError:
                            raise  # let the shutdown block handle it
                        except Exception as task_err:
                            # A single task failing (e.g. unexpected library error)
                            # must never take down the whole run.
                            logger.error(f"Unhandled task error: {task_err}")
                            continue

                        idx = row_result["row_index"]
                        results[idx] = row_result
                        checkpoint.append_result(row_result)

                        stats["processed"] += 1
                        if row_result.get("WebsiteAccessible"):
                            stats["successful"] += 1
                        else:
                            stats["failed"] += 1
                        if row_result.get("PrimaryEmail"):
                            stats["emails_found"] += 1
                        if row_result.get("LeadTier") == "HOT":
                            stats["hot"] += 1
                        elif row_result.get("LeadTier") == "WARM":
                            stats["warm"] += 1

                        if checkpoint.buffered_count() >= CONFIG.checkpoint_every:
                            checkpoint.flush()

                        if completed_count % 10 == 0 or completed_count == len(
                            pending_indices
                        ):
                            elapsed = time.monotonic() - start_time
                            rate = completed_count / elapsed if elapsed > 0 else 0
                            remaining = len(pending_indices) - completed_count
                            eta = remaining / rate if rate > 0 else 0
                            pct = stats["processed"] / total * 100
                            logger.info(
                                f"[{stats['processed']}/{total}] {pct:.1f}% | "
                                f"Emails: {stats['emails_found']} | HOT: {stats['hot']} | "
                                f"WARM: {stats['warm']} | ETA: {format_eta(eta)}"
                            )
                except (KeyboardInterrupt, asyncio.CancelledError):
                    logger.warning(
                        "Interrupted by user. Cancelling remaining tasks and saving progress..."
                    )
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
        finally:
            # No matter how we got here (success, error, interrupt), never lose
            # buffered results, and always try to release browser resources.
            checkpoint.flush()
            if context is not None:
                try:
                    await context.close()
                except Exception as e:
                    logger.debug(f"Error closing browser context: {e}")
            if browser is not None:
                try:
                    await browser.close()
                except Exception as e:
                    logger.debug(f"Error closing browser: {e}")

    return results


INVALID_EXCEL_CHAR_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\uFFFE\uFFFF\ud800-\udfff]"
)


def sanitize_excel_value(value: object) -> object:
    if isinstance(value, str):
        return INVALID_EXCEL_CHAR_RE.sub("", value)
    return value


def sanitize_dataframe_for_excel(df: pd.DataFrame) -> pd.DataFrame:
    # pandas 3.x removed DataFrame.applymap(), so use apply() per column instead.
    return df.apply(lambda col: col.apply(sanitize_excel_value))


def build_output_dataframe(df: pd.DataFrame, results: dict[int, dict]) -> pd.DataFrame:
    """Merge enrichment results back onto the original dataframe, in order."""
    for field in EMPTY_RESULT_FIELDS:
        df[f"extracted_{field}"] = [
            results.get(i, {}).get(field, "") for i in range(len(df))
        ]
    return df


def main() -> None:
    args = parse_args()

    input_path = args.input or CONFIG.input_excel

    if args.output:
        out_excel = f"{args.output}.xlsx"
        out_hot = f"{args.output}_hot_leads.xlsx"
        out_fallback = f"{args.output}_recovered.xlsx"
    else:
        out_excel = CONFIG.output_excel
        out_hot = CONFIG.hot_leads_excel
        out_fallback = "agency_leads_recovered.xlsx"

    if args.cleanstart:
        if os.path.exists(CONFIG.checkpoint_file):
            os.remove(CONFIG.checkpoint_file)
            logger.info(
                f"--cleanstart: removed existing '{CONFIG.checkpoint_file}'. "
                f"Starting from scratch."
            )
        else:
            logger.info("--cleanstart: no existing checkpoint found, starting fresh.")

    logger.info("Reading Excel file...")
    try:
        df = pd.read_excel(input_path)
    except Exception as e:
        logger.error(f"Could not read {input_path}: {e}")
        return

    target = CONFIG.website_column.lower().strip()
    matched_col = None
    for col in df.columns:
        if str(col).lower().strip() == target:
            matched_col = col
            break

    if not matched_col:
        logger.error(f"Column '{CONFIG.website_column}' not found in Excel file.")
        return

    urls = df[matched_col].tolist()
    total_sites = len(urls)

    checkpoint = CheckpointManager(CONFIG.checkpoint_file)
    already_done = {} if args.cleanstart else checkpoint.load_checkpoint()

    if already_done:
        logger.info(
            f"Loaded {total_sites} businesses. Resuming: "
            f"{len(already_done)}/{total_sites} already processed "
            f"(use --cleanstart to ignore this and start over)."
        )
    else:
        logger.info(f"Loaded {total_sites} businesses. Starting from scratch.")

    try:
        results = asyncio.run(run_crawl(urls, already_done))
    except KeyboardInterrupt:
        logger.warning("Crawl interrupted by user (Ctrl+C).")
        results = checkpoint.load_checkpoint()
    except Exception as crawl_err:
        # Whatever went wrong (browser crash, network stack failure, etc.),
        # fall back to whatever has already been checkpointed to disk so
        # 20,000 rows of work are never lost to one bad exception.
        logger.error(f"Crawl failed with an unexpected error: {crawl_err}")
        logger.error("Falling back to last saved checkpoint for output generation.")
        results = checkpoint.load_checkpoint()

    logger.info(
        "Crawl complete (or recovered from checkpoint). Building output spreadsheets..."
    )
    df = build_output_dataframe(df, results)
    safe_df = sanitize_dataframe_for_excel(df)

    try:
        safe_df.to_excel(out_excel, index=False)
        logger.info(f"Saved full enriched dataset to '{out_excel}'.")
    except Exception as e:
        logger.error(f"Failed to write {out_excel}: {e}")
        # Fall back to a safe alternate filename so results aren't lost
        safe_df.to_excel(out_fallback, index=False)
        logger.info(f"Saved to fallback file '{out_fallback}' instead.")

    try:
        hot_df = safe_df[
            safe_df["extracted_LeadScore"].apply(
                lambda x: isinstance(x, (int, float)) and x >= 60
            )
        ]
        hot_df = hot_df.sort_values("extracted_LeadScore", ascending=False)
        hot_df.to_excel(out_hot, index=False)
        logger.info(f"Saved {len(hot_df)} hot leads to '{out_hot}'.")
    except Exception as e:
        logger.error(f"Failed to write {out_hot}: {e}")

    logger.info("Done. Check 'scraper_debug.log' for detailed per-website breakdown.")


if __name__ == "__main__":
    main()
