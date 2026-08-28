"""Bounded, delayed reader for public listing-index pages."""

import time
import re
import json
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..conditions import classify_condition
from ..config import load_condition_rules
from ..models import Listing
from ..parser import parse_price

_LISTING_PATH_PARTS = ("/buy-sell/", "/articles/")


def _looks_like_price(text: str) -> bool:
    return bool(re.fullmatch(r"\s*[\d,]+\s*원\s*", text))


def _canonical_url(page_url: str, value: str) -> str:
    return urljoin(page_url, value).split("?", 1)[0]


def _index_descriptions(html: str, page_url: str) -> dict[str, str]:
    """Return public search-page JSON-LD descriptions keyed by canonical URL."""
    soup = BeautifulSoup(html, "html.parser")
    descriptions: dict[str, str] = {}
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or node.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        item_lists = payload if isinstance(payload, list) else [payload]
        for item_list in item_lists:
            if not isinstance(item_list, dict):
                continue
            for entry in item_list.get("itemListElement", []):
                product = entry.get("item", {}) if isinstance(entry, dict) else {}
                if not isinstance(product, dict):
                    continue
                url = product.get("url")
                description = product.get("description")
                if isinstance(url, str) and isinstance(description, str):
                    descriptions[_canonical_url(page_url, url)] = description.strip()
    return descriptions


def extract_public_listings(
    html: str,
    page_url: str,
    source_type: str,
    default_location: str = "",
    limit: int = 3,
) -> list[Listing]:
    """Extract only visible title, price and URL plus nearby location metadata."""
    soup = BeautifulSoup(html, "html.parser")
    descriptions = _index_descriptions(html, page_url)
    listings: list[Listing] = []
    seen_urls: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if not any(part in href for part in _LISTING_PATH_PARTS):
            continue
        url = _canonical_url(page_url, href)
        path = urlparse(url).path.rstrip("/")
        if path.endswith("/buy-sell/s"):
            continue
        if url in seen_urls:
            continue
        container = anchor.find_parent(["article", "li"]) or anchor
        title_node = container.select_one("h1, h2, h3, [data-testid*=title], .title")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        direct_spans = [
            node.get_text(" ", strip=True)
            for node in container.find_all("span")
            if node.get_text(" ", strip=True)
        ]
        if not title:
            title = next(
                (text for text in direct_spans if not _looks_like_price(text) and text != "·"),
                "",
            )
        if not title:
            image = anchor.find("img", alt=True)
            title = str(image["alt"]).strip() if image else ""
        text = container.get_text(" ", strip=True)
        price_node = container.select_one("[data-testid*=price], .price")
        fallback_price = next(
            (value for value in direct_spans if _looks_like_price(value)), None
        )
        if price_node is None and fallback_price is None:
            continue
        price_text = (
            price_node.get_text(" ", strip=True)
            if price_node
            else fallback_price
        )
        price = parse_price(price_text)
        if not title or price is None:
            continue
        location_node = container.select_one("[data-testid*=location], .location")
        location = (
            location_node.get_text(" ", strip=True) if location_node else default_location
        )
        location = location.split("·", 1)[0].strip()
        if not location or location == default_location:
            price_index = next(
                (index for index, text in enumerate(direct_spans) if _looks_like_price(text)),
                -1,
            )
            if price_index >= 0:
                location = next(
                    (
                        text
                        for text in direct_spans[price_index + 1 :]
                        if text != "·" and not _looks_like_price(text)
                    ),
                    default_location,
                )
        location = location.split("·", 1)[0].strip()
        listing_id = path.split("/")[-1] or None
        listings.append(
            Listing(
                title,
                price,
                url,
                location,
                source_type,
                listing_id,
                descriptions.get(url, ""),
            )
        )
        seen_urls.add(url)
        if len(listings) >= limit:
            break
    return listings


def extract_listing_description(html: str) -> str:
    """Read the public listing description from its detail page metadata."""
    soup = BeautifulSoup(html, "html.parser")
    description = soup.select_one('meta[name="description"]')
    return str(description.get("content", "")).strip() if description else ""


class PublicCrawler:
    def __init__(
        self,
        delay_seconds: float = 2.0,
        timeout_seconds: float = 10.0,
        user_agent: str = "karrot_pc_finder/0.1",
        sleep: Callable[[float], None] = time.sleep,
    ):
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent
        self._has_requested = False
        self.condition_rules = load_condition_rules()

    def fetch(self, url: str) -> str:
        if self._has_requested:
            self.sleep(self.delay_seconds)
        response = self.session.get(url, timeout=self.timeout_seconds)
        self._has_requested = True
        response.raise_for_status()
        if response.encoding and response.encoding.lower() in {"iso-8859-1", "latin-1"}:
            response.encoding = response.apparent_encoding
        return response.text

    def scan(
        self, url: str, source_type: str, location: str = "", limit: int = 3
    ) -> list[Listing]:
        listings = self.discover(url, source_type, location, limit)
        return [self.inspect(listing) for listing in listings]

    def discover(
        self, url: str, source_type: str, location: str = "", limit: int = 3
    ) -> list[Listing]:
        """Read ordered public result cards without fetching any detail pages."""
        return extract_public_listings(
            self.fetch(url), url, source_type, location, limit
        )

    def inspect(self, listing: Listing) -> Listing:
        """Fetch and condition-classify one new listing's detail page."""
        try:
            detail_description = extract_listing_description(self.fetch(listing.url))
            description = detail_description or listing.description
            status = classify_condition(
                listing.title,
                description,
                self.condition_rules,
                description_inspected=bool(description),
            )
        except requests.RequestException:
            # A public-index description is enough to classify safely; otherwise unknown.
            description = listing.description
            status = classify_condition(
                listing.title,
                description,
                self.condition_rules,
                description_inspected=bool(description),
            )
        return Listing(
            listing.title,
            listing.price,
            listing.url,
            listing.location,
            listing.source_type,
            listing.listing_id,
            description,
            status,
        )
