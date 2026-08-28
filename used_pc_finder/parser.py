"""Listing text parsing and conservative product-name normalization."""

import re

_SPACE = re.compile(r"\s+")

_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(?:rtx\s*)?3080\b"), "RTX 3080"),
    (re.compile(r"(?i)(?:rtx\s*)?3070\s*(?:ti|티아이)\b"), "RTX 3070 Ti"),
    (re.compile(r"(?i)(?:rtx\s*)?3070\b"), "RTX 3070"),
    (re.compile(r"(?i)(?:rtx\s*)?3060\s*(?:ti|티아이)\b"), "RTX 3060 Ti"),
    (re.compile(r"(?i)(?:rtx\s*)?4060\s*(?:ti|티아이)\b"), "RTX 4060 Ti"),
    (re.compile(r"(?i)(?:rtx\s*)?4060\b"), "RTX 4060"),
    (re.compile(r"(?i)(?:rx\s*)?7800\s*xt\b"), "RX 7800 XT"),
    (re.compile(r"(?i)(?:rx\s*)?7700\s*xt\b"), "RX 7700 XT"),
    (re.compile(r"(?i)(?:rx\s*)?6800\s*xt\b"), "RX 6800 XT"),
    (re.compile(r"(?i)(?:rx\s*)?6700\s*xt\b"), "RX 6700 XT"),
    (re.compile(r"(?i)(?:rtx\s*)?4070\s*(?:s|super)\b"), "RTX 4070 SUPER"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?7800\s*x3d\b"), "Ryzen 7 7800X3D"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?5800\s*x3d\b"), "Ryzen 7 5800X3D"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?5700\s*x3d\b"), "Ryzen 7 5700X3D"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?7700\s*x\b"), "Ryzen 7 7700X"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?7600\s*x\b"), "Ryzen 5 7600X"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?7600\b"), "Ryzen 5 7600"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?7500\s*f\b"), "Ryzen 5 7500F"),
    (re.compile(r"(?i)(?:ryzen\s*[3579]\s*|라이젠\s*[3579]?\s*)?5700\s*x\b"), "Ryzen 7 5700X"),
    (re.compile(r"(?i)(?:ryzen\s*5\s*|라이젠\s*5?\s*)?5600\s*x\b"), "Ryzen 5 5600X"),
    (re.compile(r"(?i)(?:ryzen\s*5\s*|라이젠\s*5?\s*)?5600\b"), "Ryzen 5 5600"),
    (re.compile(r"(?i)(?:core\s*)?i?7[-\s]?14700\s*k\b"), "Intel Core i7-14700K"),
    (re.compile(r"(?i)(?:core\s*)?i?5[-\s]?14600\s*k\b"), "Intel Core i5-14600K"),
    (re.compile(r"(?i)(?:core\s*)?i?7[-\s]?13700\s*k\b"), "Intel Core i7-13700K"),
    (re.compile(r"(?i)(?:core\s*)?i?5[-\s]?13600\s*k\b"), "Intel Core i5-13600K"),
    (re.compile(r"(?i)(?:core\s*)?i?7[-\s]?12700\s*k\b"), "Intel Core i7-12700K"),
    (re.compile(r"(?i)(?:core\s*)?i?5[-\s]?12600\s*k\b"), "Intel Core i5-12600K"),
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
    for pattern, canonical_name in _NORMALIZERS:
        if pattern.search(cleaned):
            return canonical_name
    return None


def is_computer_part(title: str) -> bool:
    """Keep only titles containing a recognizable computer-part term."""
    return bool(_PART_TERMS.search(title))


def parse_price(text: str) -> int | None:
    """Parse a KRW display price; return None for free/negotiable listings."""
    if re.search(r"무료|나눔|가격\s*제안|협의", text):
        return None
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None
