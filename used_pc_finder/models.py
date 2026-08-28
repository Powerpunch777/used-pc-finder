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


@dataclass(frozen=True, slots=True)
class Deal:
    listing: Listing
    normalized_name: str
    reference_price: int
    discount_percent: float
