"""Public, unauthenticated Bunjang latest-search and detail-page reader."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import requests

from .conditions import classify_condition
from .config import load_condition_rules
from .models import Listing

SEARCH_URL = "https://api.bunjang.co.kr/api/search/v8/web/search"
DETAIL_URL = "https://api.bunjang.co.kr/api/pms/v1/products/{product_id}/detail/web"
PUBLIC_WEB_ORIGIN = "https://m.bunjang.co.kr"
MARKETPLACE = "bunjang"


@dataclass(frozen=True, slots=True)
class BunjangPage:
    listings: list[Listing]
    next_cursor: str | None
    is_monotonic_descending: bool
    over_budget_count: int = 0
    search_record_count: int = 0


def _fingerprint(record: dict[str, Any]) -> str:
    values = {
        "name": record.get("name", ""),
        "status": record.get("status", ""),
        "category": record.get("category", ""),
    }
    return hashlib.sha256(
        json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class BunjangCrawler:
    """Use only the same public latest-sort functionality exposed by Bunjang web."""

    def __init__(
        self,
        delay_seconds: float = 2.0,
        timeout_seconds: float = 15.0,
        user_agent: str = "used_pc_finder/0.1 (personal public-page reader)",
        maximum_listing_price: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        if maximum_listing_price is not None and maximum_listing_price <= 0:
            raise ValueError("maximum_listing_price must be positive")
        self.maximum_listing_price = maximum_listing_price
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Origin": PUBLIC_WEB_ORIGIN,
                "Referer": f"{PUBLIC_WEB_ORIGIN}/search/products",
            }
        )
        self._has_requested = False
        self.condition_rules = load_condition_rules()
        self.detail_requests = 0

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        if self._has_requested:
            self.sleep(self.delay_seconds)
        response = self.session.get(url, params=params, timeout=self.timeout_seconds)
        self._has_requested = True
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bunjang returned a non-object JSON response")
        return payload

    def search_page(self, query: str, source_key: str, cursor: str | None = None) -> BunjangPage:
        """Fetch one public `sort=latest` page and exclude non-product/ad records."""
        params = {
            "policyKey": "pw.product.keyword",
            "q": query,
            "sort": "latest",
            "size": "60",
        }
        if cursor:
            params["cursor"] = cursor
        payload = self._get(SEARCH_URL, params=params)
        response = payload["data"]["responses"]["mainGrid"]["searchResponse"]
        records = response.get("data", [])
        listings: list[Listing] = []
        over_budget_count = 0
        for record in records:
            if (
                not isinstance(record, dict)
                or record.get("type") != "PRODUCT"
                or record.get("status") != "SELLING"
                or record.get("ad") is True
                or not record.get("pid")
                or not record.get("updatedAt")
            ):
                continue
            try:
                price = int(record["price"])
            except (KeyError, TypeError, ValueError):
                continue
            if self.maximum_listing_price is not None and price > self.maximum_listing_price:
                over_budget_count += 1
                continue
            product_id = str(record["pid"])
            listings.append(
                Listing(
                    title=str(record.get("name", "")).strip(),
                    price=price,
                    url=f"{PUBLIC_WEB_ORIGIN}/products/{product_id}",
                    location="",
                    source_type="bunjang_search",
                    listing_id=f"{MARKETPLACE}:{product_id}",
                    marketplace=MARKETPLACE,
                    product_id=product_id,
                    source_key=source_key,
                    updated_at=str(record["updatedAt"]),
                    search_fingerprint=_fingerprint(record),
                )
            )
        times = [listing.updated_at for listing in listings if listing.updated_at]
        return BunjangPage(
            listings,
            str(response["cursor"]) if response.get("cursor") else None,
            all(times[index] >= times[index + 1] for index in range(len(times) - 1)),
            over_budget_count,
            len(records),
        )

    def inspect(self, listing: Listing) -> Listing:
        """Fetch details only after the database marks a Bunjang record new or changed."""
        if listing.product_id is None:
            raise ValueError("Bunjang detail request requires product_id")
        self.detail_requests += 1
        payload = self._get(DETAIL_URL.format(product_id=listing.product_id))
        product = payload["data"]["product"]
        description = str(product.get("description", "")).strip()
        title = str(product.get("name", listing.title)).strip()
        price = int(product.get("price", listing.price))
        status = classify_condition(title, description, self.condition_rules)
        return replace(
            listing,
            title=title,
            price=price,
            description=description,
            condition_status=status,
            # The search response is the public incremental-scan authority. Its
            # timestamps have coarser precision than the detail endpoint, so
            # replacing this value would make an unchanged product look changed.
            updated_at=listing.updated_at,
        )
