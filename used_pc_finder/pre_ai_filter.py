"""Cheap deterministic rejections that avoid unnecessary AI calls."""

from __future__ import annotations

import re

from .models import Listing
from .parser import is_computer_part

_ACCESSORY_ONLY = re.compile(
    r"(?i)\\b(?:box\\s*only|empty\\s*box|accessor(?:y|ies)\\s*only|"
    r"cable\\s*only|adapter\\s*only|manual\\s*only|for\\s*parts|parts\\s*only)\\b|"
    r"박스\\s*(?:만|only)|상자\\s*만|케이블\\s*만|어댑터\\s*만|"
    r"설명서\\s*만|부품\\s*(?:용|만|only)|본체\\s*없",
)


def cheap_rejection_reason(listing: Listing) -> str | None:
    """Return a deterministic reject reason before the Codex CLI is invoked."""
    if listing.condition_status != "normal":
        return f"rule-based condition status is {listing.condition_status}"
    text = f"{listing.title}\n{listing.description}"
    if _ACCESSORY_ONLY.search(text):
        return "rule-based accessory, box, or parts-only match"
    if not is_computer_part(text):
        return "rule-based computer-part match missing"
    return None
