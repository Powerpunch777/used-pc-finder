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
# Keep the broad system terms separate from title evidence.  A standalone
# graphics card often says that it was "removed from a PC case" (본체에서
# 분리), which is not evidence that the listing itself is a complete PC.
_COMPLETE_PC = re.compile(
    r"(?i)(?:완본체|컴퓨터\s*본체|게이밍\s*컴퓨터|데스크탑|desktop|complete\s*pc|\bpc\s*본체\b)"
)
_TITLE_COMPLETE_PC = re.compile(
    r"(?i)(?:완본체|어항\s*본체|감성\s*본체|고사양\s*본체|신품\s*본체|"
    r"겜\s*본체|게이밍\s*(?:pc|컴퓨터|데스크탑|본체)|"
    r"고사양\s*(?:pc|컴퓨터|데스크탑)|"
    r"조립\s*(?:pc|컴퓨터|데스크탑|본체)|"
    r"(?:pc|컴퓨터|데스크탑)\s*(?:판매|팝니다|입니다|본체)|"
    r"(?:판매|팝니다)\s*(?:pc|컴퓨터|데스크탑|본체)|"
    r"(?:^|\s)본체(?:\s|$))"
)
_LAPTOP_TERMS = re.compile(r"(?i)(?:노트북|laptop|macbook|맥북)")
_LAPTOP_MODEL = re.compile(
    r"(?i)(?:\b(?:asus\s+)?tuf\s+gaming\s*f\d{2}\b|"
    r"\brog\s+strix\s+g\d{2}\b|제피러스|\bzephyrus\b|"
    r"프레데터|\bpredator\b|헬리오스|\bhelios\b|"
    r"\bmsi\s+(?:sword|gf)\w*\b|레이저\s*블레이드|\brazer\s+blade\b|"
    r"(?:hp\s*)?omen\b|오멘)"
)
_TITLE_NON_SALE = re.compile(
    r"(?i)(?:삽니다|구매\s*(?:원합니다|희망|합니다)|매입(?:\s*(?:원합니다|합니다))?|"
    r"구합니다|교환\s*(?:원합니다|희망|합니다)?)"
)
_TITLE_BOX_ONLY = re.compile(r"(?i)(?:^|\s)(?:그래픽카드\s*)?박스(?:\s|$)")
_TITLE_MULTI_PARTS = re.compile(r"(?i)\bpc\s*부품\b|컴퓨터\s*부품")
_BUNDLE = re.compile(
    r"(?i)(?:보드셋|세트|묶음|일괄|\bset\b|\bbundle\b|\+\s*(?:램|ram|ddr|보드|board|쿨러|cooler|b[34567]50|x[34567]70)|"
    r"(?:cpu|라이젠|ryzen).{0,30}\+.{0,30}(?:램|ram|ddr|보드|board))"
)
_SYSTEM_COMPONENT_LABEL = re.compile(
    r"(?im)(?:^|[\n/|])\s*(cpu|gpu|vga|ram|ssd|hdd|m\.?b|"
    r"메인보드|메모리|파워|power\s*supply|케이스|쿨러)\s*(?:[:：\-–—]|[·.]{2,})"
)
_SYSTEM_SPEC_SIGNALS = {
    "cpu": re.compile(r"(?i)(?:라이젠|ryzen|코어\s*i?[3579]|\bcore\s*i?[3579]\b)"),
    "memory": re.compile(r"(?i)(?:ddr[345]|\b(?:ram|memory)\b|메모리|램)"),
    "board": re.compile(r"(?i)(?:메인보드|\bm\.?b\b|\b(?:a|b|x|z|h)[0-9]{3}[a-z0-9-]*\b)"),
    "storage": re.compile(r"(?i)(?:\b(?:ssd|nvme|hdd)\b|m\.2)"),
    "graphics": re.compile(r"(?i)(?:\brtx\s*\d{4}\b|\brx\s*\d{4}\b|그래픽카드)"),
    "power_or_case": re.compile(r"(?i)(?:파워|power\s*supply|\b[5-9]\d{2,3}w\b|케이스|case\b|쿨러|cooler)"),
    "operating_system": re.compile(r"(?i)(?:windows\s*1[01]|윈도우\s*1[01]|win\s*1[01])"),
}


def _has_system_spec_inventory(text: str) -> bool:
    """Recognize a multi-part desktop specification, not part specifications.

    Complete-PC adverts regularly omit a literal "PC" but list a CPU, board,
    memory, storage, GPU, PSU, case, and cooler separated by slashes or lines.
    Requiring three independent categories and one chassis/platform category
    avoids classifying a genuine standalone GPU merely because its description
    mentions RAM or the PC it was removed from.
    """
    signals = {name for name, pattern in _SYSTEM_SPEC_SIGNALS.items() if pattern.search(text)}
    platform = {"board", "storage", "power_or_case"}
    return (
        len(signals) >= 3 and bool(signals & platform)
    ) or {"cpu", "graphics", "operating_system"}.issubset(signals)


def cheap_listing_scope(listing: Listing) -> str | None:
    """Return an unambiguous non-standalone scope without spending an AI call."""
    text = f"{listing.title}\n{listing.description}"
    title = listing.title
    if _TITLE_NON_SALE.search(title):
        # The persisted AI-scope schema intentionally has no wanted/trade
        # value.  ``unknown`` remains a fail-closed non-priceable scope.
        return "unknown"
    if _LAPTOP_TERMS.search(text) or _LAPTOP_MODEL.search(text):
        return "unknown"
    if _TITLE_BOX_ONLY.search(title) and not re.search(r"(?i)풀박스|full\s*box|포함|미개봉", title):
        return "accessory"
    if _TITLE_MULTI_PARTS.search(title):
        return "bundle"
    if _ACCESSORY_ONLY.search(text):
        return "accessory"
    if _TITLE_COMPLETE_PC.search(title) or _COMPLETE_PC.search(text):
        return "complete_pc"
    # Retail complete-PC adverts commonly list CPU ··· / RAM ··· / GPU ···
    # without saying "본체".  Two or more distinct component labels are
    # decisive system evidence, while a normal component listing may mention
    # one specification label.
    labels = {match.group(1).casefold() for match in _SYSTEM_COMPONENT_LABEL.finditer(text)}
    if len(labels) >= 2:
        return "complete_pc"
    if _has_system_spec_inventory(text):
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
