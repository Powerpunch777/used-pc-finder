"""Reference-price comparison helpers."""

from collections.abc import Iterable, Mapping

from .models import Deal, Listing
from .parser import is_computer_part, normalize_product_name
from .pre_ai_filter import cheap_rejection_reason

_BUNDLE_MISMATCH_TERMS = ("bundle", "묶음", "일괄", "세트", "set ")


def comparable_product_name(listing: Listing, *, require_ai: bool = False) -> str | None:
    """Return a normalized name only for listings safe to use as market evidence."""
    if (
        listing.listing_status != "active"
        or listing.ai_sale_status not in {"active", "unknown"}
        or listing.ai_scope != "standalone"
    ):
        return None
    if listing.ai_reject or listing.ai_is_computer_part is False:
        return None
    if require_ai and listing.ai_is_computer_part is not True:
        return None
    normalized = listing.ai_normalized_product_name or normalize_product_name(listing.title)
    rejection_reason = cheap_rejection_reason(listing)
    if rejection_reason and not (
        normalized is not None and rejection_reason == "rule-based computer-part match missing"
    ):
        return None
    text = f"{listing.title}\n{listing.description}".lower()
    if any(term in text for term in _BUNDLE_MISMATCH_TERMS):
        return None
    if normalized is None and not is_computer_part(listing.title):
        return None
    return normalized


def discount_percent(listing_price: int, reference_price: int) -> float:
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")
    return (reference_price - listing_price) / reference_price * 100.0


def find_deals(
    listings: Iterable[Listing],
    market_prices: Mapping[str, int],
    minimum_discount_percent: float,
    *,
    require_ai: bool = False,
) -> list[Deal]:
    deals: list[Deal] = []
    for listing in listings:
        normalized = comparable_product_name(listing, require_ai=require_ai)
        if normalized is None or normalized not in market_prices:
            continue
        reference = market_prices[normalized]
        discount = discount_percent(listing.price, reference)
        if discount >= minimum_discount_percent:
            deals.append(Deal(listing, normalized, reference, discount))
    return sorted(deals, key=lambda deal: deal.discount_percent, reverse=True)
