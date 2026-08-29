"""Cheap deterministic rejections that avoid unnecessary AI calls."""

from __future__ import annotations

import re

from .models import Listing
from .parser import is_computer_part, normalize_product_name

_ACCESSORY_ONLY = re.compile(
    r"(?i)\\b(?:box\\s*only|empty\\s*box|accessor(?:y|ies)\\s*only|"
    r"cable\\s*only|adapter\\s*only|manual\\s*only|for\\s*parts|parts\\s*only)\\b|"
    r"박스\\s*(?:만|only)|상자\\s*만|케이블\\s*만|어댑터\\s*만|"
    r"설명서\\s*만|부품\\s*(?:용|만|only)|본체\\s*없",
)
_COMPLETE_PC = re.compile(
    r"(?i)(?:완본체|컴퓨터\s*본체|게이밍\s*컴퓨터|데스크탑|\b본체\b|desktop|complete\s*pc|\bpc\s*본체\b)"
)
_BUNDLE = re.compile(
    r"(?i)(?:보드셋|메인보드|motherboard|\b(?:b[34567]50|x[34567]70)\b|"
    r"(?:cpu|라이젠|ryzen).{0,30}\+.{0,30}(?:램|ram|ddr|보드|board))"
)


def cheap_listing_scope(listing: Listing) -> str | None:
    """Return an unambiguous non-standalone scope without spending an AI call."""
    text = f"{listing.title}\n{listing.description}"
    if _ACCESSORY_ONLY.search(text):
        return "accessory"
    if _COMPLETE_PC.search(text):
        return "complete_pc"
    if _BUNDLE.search(text):
        return "bundle"
    return None


def cheap_rejection_reason(listing: Listing) -> str | None:
    """Return a deterministic reject reason before the Codex CLI is invoked."""
    if listing.listing_status != "active":
        return f"rule-based listing status is {listing.listing_status}"
    if listing.condition_status != "normal":
        return f"rule-based condition status is {listing.condition_status}"
    text = f"{listing.title}\n{listing.description}"
    if scope := cheap_listing_scope(listing):
        return f"rule-based listing scope is {scope}"
    if not is_computer_part(text):
        return "rule-based computer-part match missing"
    title_name = normalize_product_name(listing.title)
    description_name = normalize_product_name(listing.description)
    if title_name and description_name and title_name != description_name:
        return "rule-based model mismatch between title and description"
    return None


def deterministic_standalone_name(listing: Listing) -> str | None:
    """Return a clear standalone model that does not require AI review.

    A model in the title is deterministic evidence only when the detail text is
    not contradicting it.  Generic titles and unsupported aliases intentionally
    return ``None`` and are the genuinely ambiguous cases for Codex.
    """
    if cheap_rejection_reason(listing) is not None:
        return None
    title_name = normalize_product_name(listing.title)
    description_name = normalize_product_name(listing.description)
    if title_name and (description_name is None or description_name == title_name):
        return title_name
    return None
