"""Incremental Bunjang scanning with changed-listing and watermark handling."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from .bunjang import BunjangCrawler, BunjangRequestError
from .database import CandidateState, ListingDatabase
from .models import Listing
from .pricing import comparable_product_name

LOGGER = logging.getLogger(__name__)


def _reliable_detail_content(listing: Listing) -> bool:
    """Do not send partial/error detail payloads to the first-stage AI."""
    return bool(listing.title.strip() and listing.description.strip() and listing.price > 0)


def _unreliable_detail_listing(listing: Listing) -> Listing:
    return replace(
        listing, condition_status="unknown", ai_is_computer_part=False,
        ai_reject=True, ai_reason="Bunjang detail content was incomplete or unreliable",
        ai_scope="unknown", ai_sale_status="unknown", ai_usable_for_market_price=False,
        ai_usable_price=False, effective_price=None,
    )


@dataclass(frozen=True, slots=True)
class BunjangScanResult:
    listings: list[Listing]
    search_records_fetched: int
    new_count: int
    updated_count: int
    pending_ai_count: int
    unchanged_count: int
    duplicate_count: int
    detail_requests: int
    pages_fetched: int
    ordering_monotonic: bool
    stopped_at_watermark: bool
    over_budget_count: int
    irrelevant_count: int
    price_observations_recorded: int
    processed_states: dict[str, CandidateState]


def scan_bunjang_source(
    crawler: BunjangCrawler,
    database: ListingDatabase,
    source: dict[str, object],
    process_listing: Callable[[Listing], Listing],
    seen_product_ids: set[str],
    *,
    record_limit: int | None = None,
) -> BunjangScanResult:
    """Process only new/changed public Bunjang records, with cautious watermark paging."""
    source_key = str(source["key"])
    query = str(source["query"])
    max_pages = int(source.get("max_pages", 2))
    overlap_pages = int(source.get("watermark_overlap_pages", 1))
    watermark = database.get_watermark("bunjang", source_key)
    cursor: str | None = None
    previous_page_last: str | None = None
    listings: list[Listing] = []
    records_fetched = new_count = updated_count = pending_ai_count = unchanged_count = 0
    duplicate_count = over_budget_count = irrelevant_count = 0
    price_observations_recorded = 0
    processed_states: dict[str, CandidateState] = {}
    pages_fetched = 0
    detail_before = crawler.detail_requests
    ordering_monotonic = True
    stopped_at_watermark = False
    boundary_pages = 0
    newest_successful: str | None = None
    examined = 0

    for _page_number in range(max_pages):
        try:
            page = crawler.search_page(query, source_key, cursor)
        except (BunjangRequestError, KeyError, TypeError, ValueError):
            LOGGER.exception("Unable to fetch Bunjang search for source %s", source_key)
            break
        pages_fetched += 1
        records_fetched += page.search_record_count or len(page.listings)
        over_budget_count += page.over_budget_count
        irrelevant_count += page.irrelevant_count
        page_times = [value.updated_at for value in page.listings if value.updated_at]
        if not page.is_monotonic_descending or (
            previous_page_last and page_times and previous_page_last < page_times[0]
        ):
            ordering_monotonic = False
        if page_times:
            previous_page_last = page_times[-1]

        page_all_old_unchanged = bool(page.listings) and watermark is not None
        page_has_new_or_changed = False
        for candidate in page.listings:
            if record_limit is not None and examined >= record_limit:
                break
            examined += 1
            if candidate.product_id in seen_product_ids:
                duplicate_count += 1
                continue
            if candidate.product_id is not None:
                seen_product_ids.add(candidate.product_id)
            state = database.candidate_state(candidate)
            if state.status == "unchanged":
                unchanged_count += 1
            else:
                page_has_new_or_changed = True
                try:
                    detailed = crawler.inspect(candidate)
                    processed = (
                        process_listing(detailed) if _reliable_detail_content(detailed)
                        else _unreliable_detail_listing(detailed)
                    )
                    database.store_processed(processed, state)
                    normalized_name = comparable_product_name(processed, require_ai=True)
                    should_observe = (
                        normalized_name is not None
                        and processed.product_id is not None
                        and (
                            state.status == "new"
                            or state.previous_price != processed.price
                            or not database.has_price_observation(
                                processed.marketplace,
                                processed.product_id,
                                normalized_name,
                            )
                        )
                    )
                    if should_observe and database.record_price_observation(
                        processed, normalized_name
                    ):
                        price_observations_recorded += 1
                except Exception:
                    LOGGER.exception("Unable to process Bunjang product %s", candidate.product_id)
                    page_all_old_unchanged = False
                    continue
                listings.append(processed)
                if processed.product_id:
                    processed_states[processed.product_id] = state
                if state.status == "new":
                    new_count += 1
                elif state.status == "updated":
                    updated_count += 1
                else:
                    pending_ai_count += 1
                if processed.updated_at and (
                    newest_successful is None or processed.updated_at > newest_successful
                ):
                    newest_successful = processed.updated_at
            if (
                watermark is None
                or candidate.updated_at is None
                or candidate.updated_at >= watermark
                or state.status != "unchanged"
            ):
                page_all_old_unchanged = False

        if watermark and ordering_monotonic and page_all_old_unchanged and not page_has_new_or_changed:
            boundary_pages += 1
            if boundary_pages > overlap_pages:
                stopped_at_watermark = True
                break
        else:
            boundary_pages = 0
        if record_limit is not None and examined >= record_limit:
            break
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    if newest_successful:
        database.set_watermark("bunjang", source_key, newest_successful)
    return BunjangScanResult(
        listings,
        records_fetched,
        new_count,
        updated_count,
        pending_ai_count,
        unchanged_count,
        duplicate_count,
        crawler.detail_requests - detail_before,
        pages_fetched,
        ordering_monotonic,
        stopped_at_watermark,
        over_budget_count,
        irrelevant_count,
        price_observations_recorded,
        processed_states,
    )
