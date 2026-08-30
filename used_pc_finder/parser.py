"""Listing text parsing and conservative product-name normalization."""

import re

_SPACE = re.compile(r"\s+")

_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A word boundary before "Ti" is not enough: "3080 Ti" has a boundary
    # after 3080.  The negative lookaheads keep higher-tier variants out of
    # their base-model price pools.
    (re.compile(r"(?i)(?:rtx\s*)?3080\b(?!\s*(?:ti|티아이)\b)"), "RTX 3080"),
    (re.compile(r"(?i)(?:rtx\s*)?3070\s*(?:ti|티아이)\b"), "RTX 3070 Ti"),
    (re.compile(r"(?i)(?:rtx\s*)?3070\b(?!\s*(?:ti|티아이)\b)"), "RTX 3070"),
    (re.compile(r"(?i)(?:rtx\s*)?3060\s*(?:ti|티아이)\b"), "RTX 3060 Ti"),
    (re.compile(r"(?i)(?:rtx\s*)?4060\s*(?:ti|티아이)\b"), "RTX 4060 Ti"),
    (re.compile(r"(?i)(?:rtx\s*)?4060\b(?!\s*(?:ti|티아이)\b)"), "RTX 4060"),
    (re.compile(r"(?i)(?:rx\s*)?7800\s*xt\b"), "RX 7800 XT"),
    (re.compile(r"(?i)(?:rx\s*)?7700\s*xt\b"), "RX 7700 XT"),
    (re.compile(r"(?i)(?:rx\s*)?6800\s*xt\b"), "RX 6800 XT"),
    (re.compile(r"(?i)(?:rx\s*)?6700\s*xt\b"), "RX 6700 XT"),
    (re.compile(r"(?i)(?:rtx\s*)?4070\s*(?:s|super)\b"), "RTX 4070 SUPER"),
    # Require a Ryzen/라이젠 family marker in listing prose.  Bare query
    # aliases are handled separately below, so DDR5-5600, RX 5600 XT and
    # i5-7600 cannot become Ryzen CPU observations.
    (re.compile(r"(?i)(?:ryzen(?:\s*7)?|라이젠(?:\s*7)?)\s*7800\s*x3d\b"), "Ryzen 7 7800X3D"),
    (re.compile(r"(?i)(?:ryzen(?:\s*7)?|라이젠(?:\s*7)?)\s*5800\s*x3d\b"), "Ryzen 7 5800X3D"),
    (re.compile(r"(?i)(?:ryzen(?:\s*7)?|라이젠(?:\s*7)?)\s*5700\s*x3d\b"), "Ryzen 7 5700X3D"),
    (re.compile(r"(?i)(?:ryzen(?:\s*7)?|라이젠(?:\s*7)?)\s*7700\s*x\b"), "Ryzen 7 7700X"),
    (re.compile(r"(?i)(?:ryzen(?:\s*5)?|라이젠(?:\s*5)?)\s*7600\s*x\b"), "Ryzen 5 7600X"),
    (re.compile(r"(?i)(?:ryzen(?:\s*5)?|라이젠(?:\s*5)?)\s*7600\b(?!\s*x\b)"), "Ryzen 5 7600"),
    (re.compile(r"(?i)(?:ryzen(?:\s*5)?|라이젠(?:\s*5)?)\s*7500\s*f\b"), "Ryzen 5 7500F"),
    (re.compile(r"(?i)(?:ryzen(?:\s*7)?|라이젠(?:\s*7)?)\s*5700\s*x\b(?!\s*3d\b)"), "Ryzen 7 5700X"),
    (re.compile(r"(?i)(?:ryzen(?:\s*5)?|라이젠(?:\s*5)?)\s*5600\s*x\b"), "Ryzen 5 5600X"),
    (re.compile(r"(?i)(?:ryzen(?:\s*5)?|라이젠(?:\s*5)?)\s*5600\b(?!\s*x\b)"), "Ryzen 5 5600"),
    (re.compile(r"(?i)(?:core\s*)?(?:i\s*7\s*[- ]*)?14700\s*k\b"), "Intel Core i7-14700K"),
    (re.compile(r"(?i)(?:core\s*)?(?:i\s*5\s*[- ]*)?14600\s*k\b"), "Intel Core i5-14600K"),
    (re.compile(r"(?i)(?:core\s*)?(?:i\s*7\s*[- ]*)?13700\s*k\b"), "Intel Core i7-13700K"),
    (re.compile(r"(?i)(?:core\s*)?(?:i\s*5\s*[- ]*)?13600\s*k\b"), "Intel Core i5-13600K"),
    (re.compile(r"(?i)(?:core\s*)?(?:i\s*7\s*[- ]*)?12700\s*k\b"), "Intel Core i7-12700K"),
    (re.compile(r"(?i)(?:core\s*)?(?:i\s*5\s*[- ]*)?12600\s*k\b"), "Intel Core i5-12600K"),
    (re.compile(r"(?i)ddr5\s*32\s*(?:gb|g|기가)\b"), "DDR5 32GB"),
    (re.compile(r"(?i)ddr5\s*64\s*(?:gb|g|기가)\b"), "DDR5 64GB"),
    (re.compile(r"(?i)(?:samsung\s*)?990\s*pro\s*2\s*(?:tb|테라)\b"), "Samsung 990 PRO 2TB"),
    (re.compile(r"(?i)(?:samsung\s*)?980\s*pro\s*2\s*(?:tb|테라)\b"), "Samsung 980 PRO 2TB"),
    (re.compile(r"(?i)(?:wd\s*(?:black)?\s*)?sn850x\s*2\s*(?:tb|테라)\b"), "WD Black SN850X 2TB"),
    (re.compile(r"(?i)(?:sk\s*hynix\s*)?(?:platinum\s*)?p41\s*2\s*(?:tb|테라)\b"), "SK hynix P41 2TB"),
    (re.compile(r"(?i)(?:nvme|ssd)\s*2\s*(?:tb|테라)\b"), "NVMe SSD 2TB"),
    (re.compile(r"(?i)\bb650\b"), "B650 Motherboard"),
    (re.compile(r"(?i)\bb550\b"), "B550 Motherboard"),
    (re.compile(r"(?i)\bz790\b"), "Z790 Motherboard"),
    (re.compile(r"(?i)\bb760\b"), "B760 Motherboard"),
    (
        re.compile(r"(?i)samsung\s*980(?:\s*(?:nvme|ssd))?\s*500\s*(?:gb|g)\b"),
        "Samsung 980 500GB",
    ),
    (
        re.compile(r"(?i)삼성\s*980(?:\s*(?:nvme|ssd))?\s*500\s*(?:gb|g|기가)\b"),
        "Samsung 980 500GB",
    ),
)

# Discovery queries may be terse, but a terse token must never act as evidence
# when it appears inside a longer listing.  These aliases are intentionally
# matched only when the complete supplied value is the query/model token.
_BARE_QUERY_ALIASES = {
    "7800x3d": "Ryzen 7 7800X3D", "5800x3d": "Ryzen 7 5800X3D",
    "5700x3d": "Ryzen 7 5700X3D", "7700x": "Ryzen 7 7700X",
    "7600x": "Ryzen 5 7600X", "7600": "Ryzen 5 7600",
    "7500f": "Ryzen 5 7500F", "5700x": "Ryzen 7 5700X",
    "5600x": "Ryzen 5 5600X", "5600": "Ryzen 5 5600",
    "14700k": "Intel Core i7-14700K", "14600k": "Intel Core i5-14600K",
    "13700k": "Intel Core i7-13700K", "13600k": "Intel Core i5-13600K",
    "12700k": "Intel Core i7-12700K", "12600k": "Intel Core i5-12600K",
}

# These are discovery buckets, not coherent market-price identities.  Exact
# products discovered by them (for example a 990 PRO) remain priceable.
DISCOVERY_ONLY_IDENTITIES = frozenset({
    "DDR5 32GB", "DDR5 64GB", "NVMe SSD 2TB", "B650 Motherboard",
    "B550 Motherboard", "Z790 Motherboard", "B760 Motherboard",
})

_PART_TERMS = re.compile(
    r"(?i)\b(?:rtx|gtx|radeon|ryzen|intel|core\s+i[3579]|ram|ddr[345]|"
    r"ssd|hdd|nvme|motherboard|mainboard|psu|cooler|4070|4060|3080|3070|3060|"
    r"7800x3d|5800x3d|5700x3d|7700x|7600x|7600|7500f|5700x|5600x|5600|"
    r"14700k|14600k|13700k|13600k|12700k|12600k|b650|b550|z790|b760)\b|"
    r"그래픽\s*카드|그래픽카드|라이젠|메모리|램|하드(?:디스크)?|메인보드|파워|쿨러|삼성\s*980"
)


def normalize_product_name(title: str) -> str | None:
    """Return a canonical known product name, or None when no alias matches."""
    cleaned = _SPACE.sub(" ", title.strip())
    if alias := _BARE_QUERY_ALIASES.get(cleaned.casefold()):
        return alias
    for pattern, canonical_name in _NORMALIZERS:
        if pattern.search(cleaned):
            return canonical_name
    return None


def is_pricing_identity(normalized_name: str | None) -> bool:
    """Whether a canonical name is specific enough for one price distribution."""
    return normalized_name is not None and normalized_name not in DISCOVERY_ONLY_IDENTITIES


def exact_model_match(normalized_name: str, title: str, description: str = "") -> bool:
    """Require the repaired canonical identity to be evidenced by listing text."""
    return (
        normalize_product_name(title) == normalized_name
        or normalize_product_name(description) == normalized_name
    )


def is_computer_part(title: str) -> bool:
    """Keep only titles containing a recognizable computer-part term."""
    return bool(_PART_TERMS.search(title))


def parse_price(text: str) -> int | None:
    """Parse a KRW display price; return None for free/negotiable listings."""
    if re.search(r"무료|나눔|가격\s*제안|협의", text):
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None
