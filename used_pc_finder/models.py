"""Shared immutable data records."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Listing:
    title: str
    price: int
    url: str
    location: str
    source_type: str
    listing_id: str | None = None
    description: str = ""
    condition_status: str = "unknown"
    ai_is_computer_part: bool | None = None
    ai_normalized_product_name: str | None = None
    ai_confidence: float | None = None
    ai_reject: bool = False
    ai_reason: str = ""
    marketplace: str = "karrot"
    product_id: str | None = None
    source_key: str = ""
    updated_at: str | None = None
    search_fingerprint: str = ""
    canonical_url: str | None = None
    listing_status: str = "active"
    ai_scope: str = "unknown"
    ai_sale_status: str = "unknown"
    ai_usable_for_market_price: bool = False
    last_active_at: str | None = None
    first_sold_seen_at: str | None = None
    last_active_price: int | None = None
    # The marketplace-displayed price remains ``price``.  AI may extract a
    # different effective price only when it is unambiguous for this exact part.
    effective_price: int | None = None
    ai_usable_price: bool = False


@dataclass(frozen=True, slots=True)
class Deal:
    listing: Listing
    normalized_name: str
    reference_price: int
    discount_percent: float
    effective_price: int | None = None
