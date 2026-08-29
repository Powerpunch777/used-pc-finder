"""Public, unauthenticated Bunjang latest-search and detail-page reader."""

from __future__ import annotations

import hashlib
import json
import logging
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
LOGGER = logging.getLogger(__name__)


def listing_status(value: object) -> str:
    """Map Bunjang's public lifecycle labels to the pipeline's stable states."""
    status = str(value or "").upper()
    if status in {"SELLING", "ACTIVE", "ON_SALE"}:
        return "active"
    if status in {"RESERVED", "RESERVATION", "RESERVING"}:
        return "reserved"
    if status in {"SOLD", "SOLD_OUT", "COMPLETED", "SALE_COMPLETED"}:
        return "sold"
    return "unavailable"


class BunjangRequestError(RuntimeError):
    """A public Bunjang request that could not be completed.

    The attributes intentionally survive the request layer so durable backfill
    retries can distinguish a deleted listing from a temporary outage.
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        exception_type: str | None = None,
        error_category: str = "unknown",
        retry_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exception_type = exception_type or type(self).__name__
        self.error_category = error_category
        # Number of bounded, in-request retries attempted before this failure.
        self.retry_count = retry_count


def detail_error_diagnostics(error: BaseException) -> tuple[int | None, str, str, int, str]:
    """Return stable, durable diagnostics for a failed detail request."""
    if isinstance(error, BunjangRequestError):
        return (
            error.http_status,
            error.exception_type,
            error.error_category,
            error.retry_count,
            str(error),
        )
    return None, type(error).__name__, "unknown", 0, str(error)


def is_transient_detail_error(error: BaseException) -> bool:
    """Whether a failed detail request may be retried on a later backfill run."""
    _status, _type, category, _retry_count, _message = detail_error_diagnostics(error)
    return category in {"read_timeout", "connect_timeout", "429", "5xx"}


def _is_retryable_request_error(error: requests.RequestException) -> bool:
    if isinstance(error, (requests.Timeout, requests.ConnectionError)):
        return True
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return error.response.status_code == 429 or error.response.status_code >= 500
    return False


def _request_error_category(error: requests.RequestException) -> tuple[int | None, str]:
    """Classify request failures without depending on provider-specific text."""
    if isinstance(error, requests.ConnectTimeout):
        return None, "connect_timeout"
    if isinstance(error, requests.ReadTimeout):
        return None, "read_timeout"
    if isinstance(error, requests.Timeout):
        return None, "read_timeout"
    if isinstance(error, requests.ConnectionError):
        # Requests does not always distinguish a refused/reset connection from
        # a connect timeout. They share the same transient retry policy.
        return None, "connect_timeout"
    if isinstance(error, requests.HTTPError) and error.response is not None:
        status = int(error.response.status_code)
        if status == 429:
            return status, "429"
        if status >= 500:
            return status, "5xx"
        if status == 404:
            return status, "404_or_unavailable"
        if 400 <= status < 500:
            return status, "other_4xx"
        return status, "unknown"
    return None, "unknown"


@dataclass(frozen=True, slots=True)
class BunjangPage:
    listings: list[Listing]
    next_cursor: str | None
    is_monotonic_descending: bool
    over_budget_count: int = 0
    irrelevant_count: int = 0
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
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
    ):
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
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
        self.request_retries = 0
        self.permanent_failures = 0
        self.request_failures = 0

    def _get(self, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        if self._has_requested:
            self.sleep(self.delay_seconds)
        self._has_requested = True
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Bunjang returned a non-object JSON response")
                return payload
            except requests.RequestException as error:
                retryable = _is_retryable_request_error(error)
                if retryable and attempt < self.max_retries:
                    delay = self.retry_backoff_seconds * (2 ** attempt)
                    self.request_retries += 1
                    LOGGER.warning(
                        "Retrying Bunjang request after %s (attempt %s/%s, backoff %.1fs)",
                        error,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    self.sleep(delay)
                    continue
                self.request_failures += 1
                if not retryable:
                    self.permanent_failures += 1
                http_status, category = _request_error_category(error)
                raise BunjangRequestError(
                    str(error),
                    http_status=http_status,
                    exception_type=type(error).__name__,
                    error_category=category,
                    retry_count=attempt,
                ) from error

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
        irrelevant_count = 0
        for record in records:
            if (
                not isinstance(record, dict)
                or record.get("type") != "PRODUCT"
                or record.get("ad") is True
                or not record.get("pid")
                or not record.get("updatedAt")
            ):
                irrelevant_count += 1
                continue
            try:
                price = int(record["price"])
            except (KeyError, TypeError, ValueError):
                irrelevant_count += 1
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
                    canonical_url=f"{PUBLIC_WEB_ORIGIN}/products/{product_id}",
                    listing_status=listing_status(record.get("status")),
                )
            )
        times = [listing.updated_at for listing in listings if listing.updated_at]
        return BunjangPage(
            listings,
            str(response["cursor"]) if response.get("cursor") else None,
            all(times[index] >= times[index + 1] for index in range(len(times) - 1)),
            over_budget_count,
            irrelevant_count,
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
            listing_status=listing_status(product.get("status", listing.listing_status)),
            # The search response is the public incremental-scan authority. Its
            # timestamps have coarser precision than the detail endpoint, so
            # replacing this value would make an unchanged product look changed.
            updated_at=listing.updated_at,
        )
