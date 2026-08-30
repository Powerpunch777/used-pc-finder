"""Fail-closed text-only final email gate for Bunjang bargains."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .bunjang import BunjangCrawler, listing_status
from .database import ListingDatabase
from .models import Deal, Listing
from .parser import exact_model_match, normalize_product_name
from .pricing import discount_percent

_SCOPES = frozenset({"standalone", "bundle", "complete_pc", "accessory", "unknown"})
_CONDITIONS = frozenset({"normal", "risky", "broken", "unknown"})
_SALE_STATUSES = frozenset({"active", "reserved", "sold", "unavailable", "unknown"})


@dataclass(frozen=True, slots=True)
class FinalReviewResult:
    exact_product: bool
    normalized_product_name: str | None
    scope: str
    condition: str
    sale_status: str
    model_mismatch: bool
    displayed_price: int
    effective_price: int | None
    price_bait: bool
    hidden_price_condition: bool
    usable_price: bool
    price_confidence: float
    confidence: float
    reason: str


@dataclass(frozen=True, slots=True)
class FinalReviewAttempt:
    result: FinalReviewResult | None
    execution_seconds: float
    error: str | None


@dataclass(frozen=True, slots=True)
class FinalReviewDecision:
    deal: Deal
    passed: bool
    status: str
    reason: str
    cached: bool = False
    image_count: int = 0


@dataclass(frozen=True, slots=True)
class FinalReviewSummary:
    passed_deals: tuple[Deal, ...]
    decisions: tuple[FinalReviewDecision, ...]


def final_review_fingerprint(listing: Listing, deal: Deal) -> str:
    """Fingerprint all current text and price facts used by the second AI stage."""
    payload = {
        "review_schema": "pricing-semantics-v4",
        "product_id": listing.product_id,
        "title": listing.title,
        "description": listing.description,
        "displayed_price": listing.price,
        "effective_price": deal.effective_price,
        "sale_status": listing.listing_status,
        "normalized_product_name": deal.normalized_name,
        "reference_market_price": deal.reference_price,
        "discount_percent": round(deal.discount_percent, 6),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _prompt(listing: Listing, deal: Deal) -> str:
    return f"""Perform an independent, text-only final review of this current Bunjang listing.
Use only the supplied current detail. Do not use tools, commands, web access, browser
automation, files, images, or external knowledge. Return only JSON matching the schema.
This is factual verification only. Do not decide whether to email or reject a listing.

Listing title: {listing.title}
Full listing description: {listing.description}
Displayed marketplace price (KRW): {listing.price}
Effective price established by first-stage review (KRW): {deal.effective_price}
Normalized exact product: {deal.normalized_name}
Reference market price (KRW): {deal.reference_price}
Discount qualification percent: {deal.discount_percent:.2f}
Current marketplace sale status: {listing.listing_status}

Set exact_product true only when the supplied text supports precisely the normalized
product, not a compatible, similar, or different model. Return normalized_product_name
as the exact canonical model supported by the current text. scope must describe the
offered item. model_mismatch is true for any different model. Set displayed_price to the
current marketplace price. Set effective_price only when one fixed price applies to the
exact standalone target; otherwise set it null and usable_price false. Set price_bait
true only for clear placeholder/deceptive pricing: contact-only price, deposit,
deliberately incorrect displayed price, or text explicitly replacing the marketplace
price. Multiple products, numbered items, and multiple prices alone are not bait. If
the listing identifies one exact model and says multiple units of that model are
available, a numbered price for one unit is a one-to-one price for that exact model
unless the text identifies different variants or conditions. For example, “Samsung
980 PRO 2TB ... two units ... No. 2: 340,000 KRW” establishes an effective price of
340,000 KRW for one standalone Samsung 980 PRO 2TB. Use that effective price even when
other items are listed. Set hidden_price_condition true only if the target price is
genuinely ambiguous, conditional, negotiable without a fixed amount, bundle-dependent,
or cannot be mapped to the target. reason must state the factual basis.
"""


def _parse_result(raw: Any) -> FinalReviewResult:
    expected = {
        "exact_product", "normalized_product_name", "scope", "condition", "sale_status",
        "model_mismatch", "displayed_price", "effective_price", "price_bait",
        "hidden_price_condition", "usable_price", "price_confidence", "confidence", "reason",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise ValueError("final review does not match required keys")
    confidence = raw["confidence"]
    boolean_keys = {"exact_product", "model_mismatch", "price_bait", "hidden_price_condition", "usable_price"}
    if (
        any(not isinstance(raw[key], bool) for key in boolean_keys)
        or not (isinstance(raw["normalized_product_name"], str) or raw["normalized_product_name"] is None)
        or raw["scope"] not in _SCOPES
        or raw["condition"] not in _CONDITIONS
        or raw["sale_status"] not in _SALE_STATUSES
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or isinstance(raw["price_confidence"], bool)
        or not isinstance(raw["price_confidence"], (int, float))
        or not 0 <= raw["price_confidence"] <= 1
        or isinstance(raw["displayed_price"], bool)
        or not isinstance(raw["displayed_price"], int) or raw["displayed_price"] <= 0
        or not (isinstance(raw["effective_price"], int) or raw["effective_price"] is None)
        or isinstance(raw["effective_price"], bool)
        or (raw["effective_price"] is not None and raw["effective_price"] <= 0)
        or not isinstance(raw["reason"], str)
    ):
        raise ValueError("final review has invalid field values")
    return FinalReviewResult(
        raw["exact_product"], raw["normalized_product_name"].strip() if raw["normalized_product_name"] else None,
        raw["scope"], raw["condition"], raw["sale_status"], raw["model_mismatch"],
        raw["displayed_price"], raw["effective_price"], raw["price_bait"],
        raw["hidden_price_condition"], raw["usable_price"], float(raw["price_confidence"]),
        float(confidence), raw["reason"].strip(),
    )


class CodexTextFinalReviewer:
    """Run one read-only ephemeral Codex text review for a qualified candidate."""

    def __init__(
        self, schema_path: str | Path, *, command: str = "codex",
        model: str = "gpt-5.6-terra", reasoning_effort: str = "high",
        timeout_seconds: float = 180.0,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.schema_path = str(Path(schema_path).resolve())
        self.command = command
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.runner = runner
        self.calls = 0

    def review_attempt(self, listing: Listing, deal: Deal) -> FinalReviewAttempt:
        self.calls += 1
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="used-pc-final-codex-") as directory:
            output_path = Path(directory) / "final-review.json"
            args = [
                self.command, "-a", "never", "exec", "--ephemeral",
                "--skip-git-repo-check", "--color", "never", "-s", "read-only",
                "-C", directory, "--model", self.model,
                "-c", f'model_reasoning_effort="{self.reasoning_effort}"',
                "--output-schema", self.schema_path, "--output-last-message", str(output_path), "-",
            ]
            try:
                completed = self.runner(
                    args, cwd=directory, text=True, capture_output=True,
                    timeout=self.timeout_seconds, check=False, input=_prompt(listing, deal),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return FinalReviewAttempt(None, time.monotonic() - started, str(exc))
            if completed.returncode != 0:
                return FinalReviewAttempt(
                    None, time.monotonic() - started,
                    completed.stderr.strip() or f"Codex CLI exited {completed.returncode}",
                )
            try:
                raw = json.loads(output_path.read_text(encoding="utf-8"))
                return FinalReviewAttempt(_parse_result(raw), time.monotonic() - started, None)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                return FinalReviewAttempt(None, time.monotonic() - started, str(exc))


class FinalEmailReviewGate:
    """Refetch and independently review only already-qualified Bunjang bargains."""

    def __init__(
        self, database: ListingDatabase, crawler: BunjangCrawler,
        reviewer: CodexTextFinalReviewer, settings: Mapping[str, Any],
    ):
        self.database = database
        self.crawler = crawler
        self.reviewer = reviewer
        self.settings = settings
        self.minimum_confidence = float(settings["confidence_threshold"])

    def review_deals(self, deals: Sequence[Deal], *, minimum_discount_percent: float) -> FinalReviewSummary:
        decisions = tuple(
            self.review_deal(deal, minimum_discount_percent=minimum_discount_percent)
            for deal in deals if not self.database.was_notified(deal.listing)
        )
        return FinalReviewSummary(tuple(item.deal for item in decisions if item.passed), decisions)

    def review_deal(self, deal: Deal, *, minimum_discount_percent: float) -> FinalReviewDecision:
        listing = deal.listing
        if (
            listing.listing_status != "active" or listing.ai_scope != "standalone"
            or listing.condition_status != "normal" or listing.ai_reject
            or listing.ai_is_computer_part is not True or not listing.ai_usable_for_market_price
            or not listing.ai_usable_price or deal.effective_price is None
            or (listing.ai_confidence or 0.0) < self.minimum_confidence
        ):
            return self._record_precheck(deal, "listing did not pass first-stage bargain filters")
        if listing.marketplace != "bunjang" or not listing.product_id:
            return self._record_precheck(deal, "unsupported final-review listing")
        if deal.discount_percent < minimum_discount_percent:
            return self._record_precheck(deal, "listing no longer meets final-review discount threshold")
        try:
            refreshed = self._refreshed_listing(listing, self.crawler.fetch_product_detail(listing.product_id))
        except Exception as exc:
            return self._record_precheck(deal, f"current Bunjang detail request failed: {exc}")
        if refreshed.listing_status != "active":
            return self._record_precheck(deal, "current Bunjang listing is not active", listing=refreshed)
        if refreshed.price != listing.price:
            return self._record_precheck(deal, "current Bunjang displayed price changed", listing=refreshed)
        if not exact_model_match(deal.normalized_name, refreshed.title, refreshed.description):
            return self._record_precheck(deal, "current Bunjang detail no longer matches product identity", listing=refreshed)
        current_discount = discount_percent(deal.effective_price, deal.reference_price)
        if current_discount < minimum_discount_percent:
            return self._record_precheck(deal, "effective price no longer meets discount threshold", listing=refreshed)
        refreshed_deal = Deal(refreshed, deal.normalized_name, deal.reference_price, current_discount, deal.effective_price)
        fingerprint = final_review_fingerprint(refreshed, refreshed_deal)
        if cached := self.database.cached_final_email_review(
            refreshed, fingerprint, model=self.reviewer.model, reasoning_effort=self.reviewer.reasoning_effort,
        ):
            result = self._result_from_row(cached)
            return FinalReviewDecision(refreshed_deal, self._passes(result, refreshed_deal), str(cached["review_status"]), result.reason, cached=True)
        attempt = self.reviewer.review_attempt(refreshed, refreshed_deal)
        if attempt.result is None:
            self.database.record_final_email_review(
                refreshed, fingerprint, model=self.reviewer.model, reasoning_effort=self.reviewer.reasoning_effort,
                review=None, reviewed_price=refreshed.price, image_count=0, review_status="failed",
                execution_duration_seconds=attempt.execution_seconds, error_reason=attempt.error,
            )
            return FinalReviewDecision(refreshed_deal, False, "failed", attempt.error or "text AI review failed")
        passed = self._passes(attempt.result, refreshed_deal)
        status = "approved" if passed else "rejected"
        self.database.record_final_email_review(
            refreshed, fingerprint, model=self.reviewer.model, reasoning_effort=self.reviewer.reasoning_effort,
            review=attempt.result, reviewed_price=refreshed.price, image_count=0, review_status=status,
            execution_duration_seconds=attempt.execution_seconds,
        )
        return FinalReviewDecision(refreshed_deal, passed, status, attempt.result.reason)

    def _record_precheck(self, deal: Deal, reason: str, *, listing: Listing | None = None) -> FinalReviewDecision:
        current = listing or deal.listing
        if current.product_id:
            self.database.record_final_email_review(
                current, "precheck", model=self.reviewer.model, reasoning_effort=self.reviewer.reasoning_effort,
                review=None, reviewed_price=current.price, image_count=0, review_status="precheck_failed",
                execution_duration_seconds=0.0, error_reason=reason,
            )
        return FinalReviewDecision(deal, False, "precheck_failed", reason)

    @staticmethod
    def _refreshed_listing(listing: Listing, product: Mapping[str, Any]) -> Listing:
        return replace(
            listing, title=str(product.get("name", listing.title)).strip(),
            description=str(product.get("description", "")).strip(),
            price=int(product.get("price", listing.price)),
            listing_status=listing_status(product.get("saleStatus", product.get("status"))),
        )

    @staticmethod
    def _result_from_row(row: Mapping[str, Any]) -> FinalReviewResult:
        return FinalReviewResult(
            bool(row["exact_product"]), str(row["normalized_product_name"]) if row["normalized_product_name"] else None,
            str(row["scope"]), str(row["condition"]), str(row["sale_status"]), bool(row["model_mismatch"]),
            int(row["displayed_price"]), int(row["effective_price"]) if row["effective_price"] is not None else None,
            bool(row["price_bait"]), bool(row["hidden_price_condition"]), bool(row["usable_price"]),
            float(row["price_confidence"]), float(row["confidence"]), str(row["reason"]),
        )

    def _passes(self, result: FinalReviewResult, deal: Deal) -> bool:
        return (
            result.exact_product and result.scope == "standalone" and result.condition == "normal"
            and result.sale_status == "active" and not result.model_mismatch and result.usable_price
            and not result.price_bait and not result.hidden_price_condition
            and normalize_product_name(result.normalized_product_name or "") == deal.normalized_name
            and result.displayed_price == deal.listing.price
            and result.effective_price == deal.effective_price
            and result.price_confidence >= self.minimum_confidence
            and result.confidence >= self.minimum_confidence
        )
