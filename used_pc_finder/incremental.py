"""Incremental source processing that avoids enriching known listings."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .database import ListingDatabase
from .models import Listing


@dataclass(frozen=True, slots=True)
class SourceScanResult:
    new_listings: list[Listing]
    known_skipped: int
    consecutive_seen: int
    stopped_at_duplicate_threshold: bool


def process_source_candidates(
    candidates: Iterable[Listing],
    database: ListingDatabase,
    inspect_listing: Callable[[Listing], Listing],
    *,
    duplicate_threshold: int = 3,
    newest_first: bool = False,
) -> SourceScanResult:
    """Store and enrich new candidates while skipping known candidates cheaply.

    Early stopping is safe only for a source whose newest-first ordering has been
    independently verified.  Unverified sources still skip known detail requests,
    but continue through the configured candidate window to avoid missing a newer
    listing after an older duplicate.
    """
    if duplicate_threshold < 1:
        raise ValueError("duplicate_threshold must be at least 1")

    new_listings: list[Listing] = []
    known_skipped = 0
    consecutive_seen = 0
    for candidate in candidates:
        if database.is_known(candidate):
            known_skipped += 1
            consecutive_seen += 1
            if newest_first and consecutive_seen >= duplicate_threshold:
                return SourceScanResult(
                    new_listings,
                    known_skipped,
                    consecutive_seen,
                    True,
                )
            continue

        consecutive_seen = 0
        inspected = inspect_listing(candidate)
        if database.add(inspected):
            new_listings.append(inspected)

    return SourceScanResult(
        new_listings,
        known_skipped,
        consecutive_seen,
        False,
    )
