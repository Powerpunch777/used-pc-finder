"""Reference-price comparison helpers."""

from collections.abc import Iterable, Mapping

from .models import Deal, Listing
from .parser import is_computer_part, normalize_product_name
def comparable_product_name(listing: Listing, *, require_ai: bool = False) -> str | None:
    """Return a normalized name only for listings safe to use as market evidence."""
    if (
        listing.listing_status != "active"
        or listing.ai_sale_status not in {"active", "unknown"}
        or listing.ai_scope != "standalone"
    ):
        return None
    if (
        listing.ai_reject
        or listing.ai_is_computer_part is not True
        or not listing.ai_usable_for_market_price
        or not listing.ai_usable_price
        or listing.effective_price is None
        or listing.effective_price <= 0
    ):
        return None
    if require_ai and listing.ai_is_computer_part is not True:
        return None
    normalized = listing.ai_normalized_product_name or normalize_product_name(listing.title)
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
        effective_price = listing.effective_price
        if effective_price is None:
            continue
        discount = discount_percent(effective_price, reference)
        if discount >= minimum_discount_percent:
            deals.append(Deal(listing, normalized, reference, discount, effective_price))
    return sorted(deals, key=lambda deal: deal.discount_percent, reverse=True)
