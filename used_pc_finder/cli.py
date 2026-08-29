"""Application orchestration and text output."""

import argparse
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
import time

from .ai_classifier import (
    CLASSIFIER_VERSION,
    AIClassification,
    ClassificationAttempt,
    CodexCliClassifier,
    classification_fingerprint,
)
from .conditions import classify_condition
from .config import (
    DEFAULT_DATABASE_PATH,
    PROJECT_ROOT,
    load_condition_rules,
    load_market_prices,
    load_settings,
)
from .bunjang import BunjangCrawler, detail_error_diagnostics
from .bunjang_scan import scan_bunjang_source
from .database import ListingDatabase
from .market_estimator import (
    MarketPriceEstimate,
    PriceObservation,
    estimate_market_price,
    estimation_observations,
)
from .models import Deal, Listing
from .notifications import EmailNotifier, send_unnotified_deal_digest
from .pre_ai_filter import cheap_listing_scope, cheap_rejection_reason
from .pre_ai_filter import deterministic_standalone_name
from .pricing import comparable_product_name, discount_percent, find_deals
from .parser import normalize_product_name
from .secrets import load_smtp_password, setup_smtp_password


@dataclass(slots=True)
class AiScanStats:
    deterministic_rejects: int = 0
    candidates: int = 0
    calls: int = 0
    failures: int = 0
    accepted_normal: int = 0
    cached: int = 0
    execution_seconds: float = 0.0
    classified_listings: list[Listing] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QueuedAiClassification:
    """An uncached classification waiting for a worker-pool slot."""

    processor: "AiListingProcessor"
    listing: Listing


@dataclass(frozen=True, slots=True)
class AiTaskTiming:
    product_id: str | None
    started_at: str
    finished_at: str
    execution_seconds: float
    failed: bool


@dataclass(slots=True)
class AiPoolTelemetry:
    initial_queue_length: int = 0
    queue_length: int = 0
    active_workers: int = 0
    max_concurrency_observed: int = 0
    completed_calls: int = 0
    failures: int = 0
    cache_hits: int = 0
    subprocess_execution_seconds: float = 0.0
    wall_clock_seconds: float = 0.0
    task_timings: list[AiTaskTiming] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BackfillExclusion:
    product_id: str | None
    title: str
    price: int
    reason: str


@dataclass(frozen=True, slots=True)
class MarketPriceBackfillResult:
    product_name: str
    search_results_inspected: int
    valid_standalone_listings: int
    new_observations: int
    excluded: tuple[BackfillExclusion, ...]
    prices_used: tuple[int, ...]
    oldest_age_days: float | None
    newest_age_days: float | None
    automatic_estimate: MarketPriceEstimate
    weighted_median: int | None
    weighted_mean: int | None


@dataclass(frozen=True, slots=True)
class SaleStatusTrackingResult:
    due_listings: int
    active_listings_checked: int
    sold_transitions: int
    request_failures: int
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class FullBackfillResult:
    listings_inspected: int
    valid_observations_collected: int
    excluded_listings: int
    completed_queries: int
    incomplete_queries: int
    ai_calls: int
    ai_failures: int
    max_concurrency_observed: int
    elapsed_seconds: float


class BackfillProgressReporter:
    """Rate-smoothed progress lines for backfills and other long scans."""

    def __init__(
        self,
        total_queries: int,
        *,
        emit: Callable[[str], None] = print,
        interval_seconds: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.total_queries = total_queries
        self.emit = emit
        self.interval_seconds = interval_seconds
        self.clock = clock
        self.started = clock()
        self.last_emitted = float("-inf")
        self._recent_rates: deque[tuple[float, int]] = deque(maxlen=8)
        self._durations: deque[float] = deque(maxlen=20)

    def add_ai_duration(self, seconds: float) -> None:
        if seconds >= 0:
            self._durations.append(seconds)

    def _eta(
        self, *, completed_queries: int, processed_units: int, crawl_complete: bool,
        queue_size: int, active_workers: int, concurrency: int,
    ) -> str:
        now = self.clock()
        if crawl_complete:
            if not self._durations or concurrency < 1:
                return "calculating"
            pending = queue_size + active_workers
            return f"{(pending / concurrency) * (sum(self._durations) / len(self._durations)):.1f}"
        self._recent_rates.append((now, processed_units))
        if len(self._recent_rates) < 3 or completed_queries < 1:
            return "calculating"
        oldest_time, oldest_units = self._recent_rates[0]
        elapsed = now - oldest_time
        rate = (processed_units - oldest_units) / elapsed if elapsed > 0 else 0.0
        if rate <= 0:
            return "calculating"
        # Query sizes differ, so use recent completed-query throughput only as a
        # deliberately conservative crawl ETA until the cursor is exhausted.
        remaining_queries = max(0, self.total_queries - completed_queries)
        average_units_per_query = processed_units / max(1, completed_queries)
        return f"{(remaining_queries * average_units_per_query) / rate:.1f}"

    def report(
        self,
        *,
        completed_queries: int,
        pages_scanned: int,
        listings_inspected: int,
        valid_observations: int,
        excluded_listings: int,
        ai_queue_size: int,
        active_ai_workers: int,
        ai_completed: int,
        ai_failures: int,
        concurrency: int,
        crawl_status: str,
        crawl_complete: bool = False,
        force: bool = False,
    ) -> None:
        now = self.clock()
        if not force and now - self.last_emitted < self.interval_seconds:
            return
        self.last_emitted = now
        average = sum(self._durations) / len(self._durations) if self._durations else None
        eta = self._eta(
            completed_queries=completed_queries,
            processed_units=listings_inspected + ai_completed,
            crawl_complete=crawl_complete,
            queue_size=ai_queue_size,
            active_workers=active_ai_workers,
            concurrency=concurrency,
        )
        average_text = f"{average:.3f}" if average is not None else "calculating"
        self.emit(
            f"BACKFILL_LIVE | completed_queries={completed_queries}/{self.total_queries} | "
            f"pages_scanned={pages_scanned} | listings_inspected={listings_inspected} | "
            f"valid_observations={valid_observations} | excluded_listings={excluded_listings} | "
            f"ai_queue_size={ai_queue_size} | active_ai_workers={active_ai_workers} | "
            f"ai_completed={ai_completed} | ai_failures={ai_failures} | "
            f"rolling_avg_ai_seconds={average_text} | elapsed_seconds={now - self.started:.1f} | "
            f"crawl_status={crawl_status} | estimated_remaining_seconds={eta}"
        )


def format_deal(deal: Deal) -> str:
    item = deal.listing
    return (
        f"{deal.normalized_name} | {item.price:,} KRW | "
        f"reference {deal.reference_price:,} KRW | "
        f"{deal.discount_percent:.1f}% cheaper | {item.source_type} | "
        f"{item.location} | {item.url}"
    )


def market_price_estimate(
    database: ListingDatabase,
    normalized_name: str,
    manual_prices: dict[str, int],
    settings: dict[str, object],
) -> MarketPriceEstimate:
    options = settings["market_price_estimation"]
    return estimate_market_price(
        normalized_name,
        database.price_observations(normalized_name),
        manual_reference_price=manual_prices.get(normalized_name),
        window_days=int(options["window_days"]),
        half_life_days=float(options["half_life_days"]),
        minimum_observations=int(options["minimum_observations"]),
        estimator=str(options["estimator"]),  # validated configuration value
    )


def print_market_price_debug(estimate: MarketPriceEstimate) -> None:
    origin = "automatic" if estimate.automatic else "manual_fallback"
    price = f"{estimate.price:,} KRW" if estimate.price is not None else "unavailable"
    print(
        f"MARKET_PRICE | {estimate.normalized_product_name} | price={price} | "
        f"valid_observations={estimate.valid_observation_count} | "
        f"estimator={estimate.estimator} | source={origin} | "
        f"oldest={estimate.oldest_observed_at or '-'} | "
        f"newest={estimate.newest_observed_at or '-'}"
    )


def format_ai_listing_audit(
    listing: Listing,
    database: ListingDatabase,
    manual_prices: dict[str, int],
    settings: dict[str, object],
    minimum_discount_percent: float,
) -> str:
    """Render the final pricing decision for one AI-classified listing."""
    normalized_name = comparable_product_name(listing, require_ai=True)
    if normalized_name is None:
        return (
            f"AI_LISTING | product_id={listing.product_id or '-'} | title={listing.title} | "
            f"price={listing.price:,} KRW | normalized=- | condition={listing.condition_status} | "
            f"confidence={(listing.ai_confidence or 0.0):.2f} | "
            "reference_source=unavailable | reference_price=- | discount_percent=- | "
            "final_decision=rejected"
        )
    estimate = market_price_estimate(database, normalized_name, manual_prices, settings)
    source = "automatic" if estimate.automatic else "manual"
    if estimate.price is None:
        return (
            f"AI_LISTING | product_id={listing.product_id or '-'} | title={listing.title} | "
            f"price={listing.price:,} KRW | normalized={normalized_name} | "
            f"condition={listing.condition_status} | confidence={(listing.ai_confidence or 0.0):.2f} | "
            f"reference_source={source} | reference_price=- | discount_percent=- | "
            "final_decision=no_reference_price"
        )
    discount = (estimate.price - listing.price) / estimate.price * 100.0
    decision = "bargain" if discount >= minimum_discount_percent else "below_discount_threshold"
    return (
        f"AI_LISTING | product_id={listing.product_id or '-'} | title={listing.title} | "
        f"price={listing.price:,} KRW | normalized={normalized_name} | "
        f"condition={listing.condition_status} | confidence={(listing.ai_confidence or 0.0):.2f} | "
        f"reference_source={source} | reference_price={estimate.price:,} KRW | "
        f"discount_percent={discount:.1f} | final_decision={decision}"
    )


def effective_market_prices(
    listings: Sequence[Listing],
    database: ListingDatabase,
    manual_prices: dict[str, int],
    settings: dict[str, object],
    *,
    require_ai: bool = False,
) -> dict[str, int]:
    prices: dict[str, int] = {}
    for normalized_name in {
        name for item in listings if (name := comparable_product_name(item, require_ai=require_ai))
    }:
        estimate = market_price_estimate(database, normalized_name, manual_prices, settings)
        if estimate.price is not None:
            prices[normalized_name] = estimate.price
    return prices


def effective_market_price_estimates(
    listings: Sequence[Listing],
    database: ListingDatabase,
    manual_prices: dict[str, int],
    settings: dict[str, object],
    *,
    require_ai: bool = False,
) -> dict[str, MarketPriceEstimate]:
    """Return effective estimates so digest entries can identify their price source."""
    estimates: dict[str, MarketPriceEstimate] = {}
    for normalized_name in {
        name for item in listings if (name := comparable_product_name(item, require_ai=require_ai))
    }:
        estimate = market_price_estimate(database, normalized_name, manual_prices, settings)
        if estimate.price is not None:
            estimates[normalized_name] = estimate
    return estimates


def sample_deals() -> list[Deal]:
    settings = load_settings()
    sample = Listing(
        title="4070s 팝니다",
        price=520_000,
        url="https://m.bunjang.co.kr/products/sample-4070-super",
        location="",
        source_type="bunjang_search",
        listing_id="bunjang:sample-4070-super",
        description="정상 작동하며 이상 없습니다.",
        condition_status=classify_condition(
            "4070s 팝니다", "정상 작동하며 이상 없습니다.", load_condition_rules()
        ),
        marketplace="bunjang",
        product_id="sample-4070-super",
        source_key="sample",
        ai_is_computer_part=True,
        ai_normalized_product_name="RTX 4070 SUPER",
        ai_confidence=1.0,
        ai_scope="standalone",
    )
    return find_deals(
        [sample], load_market_prices(), settings["minimum_discount_percent"]
    )


def test_email_deals(
    database: ListingDatabase,
    manual_prices: dict[str, int],
    settings: dict[str, object],
) -> list[Deal]:
    """Build a multi-item test digest from safe, stored Bunjang listings."""
    listings = database.bunjang_listings_for_notification_test()
    if len(listings) < 2:
        raise RuntimeError("At least two classified Bunjang listings are required for a test digest")
    minimum_discount = float(settings["minimum_discount_percent"])
    qualifying_reference_multiplier = 1 / (1 - minimum_discount / 100)
    deals: list[Deal] = []
    for listing in listings:
        normalized_name = comparable_product_name(listing, require_ai=True)
        if normalized_name is None or normalized_name not in manual_prices:
            continue
        reference_price = max(
            listing.price + 1,
            int(listing.price * qualifying_reference_multiplier) + 1,
        )
        deals.append(
            Deal(
                listing,
                normalized_name,
                reference_price,
                discount_percent(listing.price, reference_price),
            )
        )
        if len(deals) == 2:
            return deals
    raise RuntimeError("No safe Bunjang listings are available for a test digest")


def classify_new_listing(
    listing: Listing,
    classifier: CodexCliClassifier | None,
    ai_settings: dict[str, object],
) -> Listing:
    """Apply cheap rules first, then fail-closed AI classification to a new listing."""
    rejection_reason = cheap_rejection_reason(listing)
    if rejection_reason:
        return replace(
            listing,
            ai_is_computer_part=False,
            ai_confidence=0.0,
            ai_reject=True,
            ai_reason=rejection_reason,
            ai_scope=cheap_listing_scope(listing) or listing.ai_scope,
        )
    deterministic_name = deterministic_standalone_name(listing)
    if deterministic_name:
        return _deterministic_accept(listing, deterministic_name)
    if classifier is None:
        return _failed_ai_listing(listing, "AI review is unavailable for an ambiguous listing")
    if hasattr(classifier, "classify_attempt"):
        attempt = classifier.classify_attempt(listing)
        result = attempt.classification
        error = attempt.error
    else:
        result = classifier.classify(listing)
        error = None
    if result is None:
        return _failed_ai_listing(listing, error or "Codex CLI classification failed")
    return _apply_ai_classification(listing, result, ai_settings)


def _apply_ai_classification(
    listing: Listing,
    result: AIClassification,
    ai_settings: dict[str, object],
) -> Listing:
    minimum_confidence = float(
        ai_settings.get("confidence_threshold", ai_settings.get("minimum_confidence", 0.85))
    )
    canonical_name = (
        normalize_product_name(result.normalized_product_name)
        if result.normalized_product_name
        else None
    )
    reject = (
        result.reject
        or not result.is_computer_part
        or result.scope != "standalone"
        or result.condition_status != "normal"
        or result.confidence < minimum_confidence
        or not canonical_name
        or result.sale_status != "active"
        or not result.usable_for_market_price
    )
    reason = result.reason
    if result.normalized_product_name and not canonical_name:
        reason = f"AI normalized product cannot be canonicalized: {result.normalized_product_name}"
    final_status = result.condition_status
    if result.confidence < minimum_confidence or not canonical_name:
        final_status = "unknown"
    return replace(
        listing,
        condition_status=final_status,
        ai_is_computer_part=result.is_computer_part,
        ai_normalized_product_name=canonical_name,
        ai_confidence=result.confidence,
        ai_reject=reject,
        ai_reason=reason,
        ai_scope=result.scope,
        ai_sale_status=result.sale_status,
        ai_usable_for_market_price=result.usable_for_market_price and not reject,
    )


def _deterministic_accept(listing: Listing, normalized_name: str) -> Listing:
    """Mark a clearly identified active standalone part without an AI call."""
    return replace(
        listing,
        condition_status="normal",
        ai_is_computer_part=True,
        ai_normalized_product_name=normalized_name,
        ai_confidence=1.0,
        ai_reject=False,
        ai_reason="rule-based clear active standalone part",
        ai_scope="standalone",
        ai_sale_status="active",
        ai_usable_for_market_price=True,
    )


def _failed_ai_listing(listing: Listing, reason: str) -> Listing:
    return replace(
        listing,
        condition_status="unknown",
        ai_is_computer_part=False,
        ai_confidence=0.0,
        ai_reject=True,
        ai_reason=reason,
        ai_sale_status="unknown",
        ai_usable_for_market_price=False,
    )


class AiListingProcessor:
    """Apply deterministic gates and cache reuse before AI queueing."""

    def __init__(
        self,
        database: ListingDatabase,
        classifier: CodexCliClassifier | None,
        ai_settings: dict[str, object],
        stats: AiScanStats,
    ):
        self.database = database
        self.classifier = classifier
        self.ai_settings = ai_settings
        self.stats = stats

    def prepare(self, candidate: Listing) -> Listing | QueuedAiClassification:
        """Return a final cheap/cached result or enqueue an uncached CLI task."""
        rejection_reason = cheap_rejection_reason(candidate)
        if rejection_reason:
            self.stats.deterministic_rejects += 1
            return replace(
                candidate,
                ai_is_computer_part=False,
                ai_confidence=0.0,
                ai_reject=True,
                ai_reason=rejection_reason,
                ai_scope=cheap_listing_scope(candidate) or candidate.ai_scope,
                ai_sale_status=candidate.listing_status,
                ai_usable_for_market_price=False,
            )
        if deterministic_name := deterministic_standalone_name(candidate):
            classified = _deterministic_accept(candidate, deterministic_name)
            self.stats.accepted_normal += 1
            self.stats.classified_listings.append(classified)
            return classified
        if self.classifier is None:
            return _failed_ai_listing(candidate, "AI review is unavailable for an ambiguous listing")
        self.stats.candidates += 1
        fingerprint = classification_fingerprint(candidate)
        cached = self.database.cached_ai_classification(
            candidate,
            fingerprint,
            model=self.classifier.model,
            reasoning_effort=self.classifier.reasoning_effort,
            classifier_version=CLASSIFIER_VERSION,
        )
        if cached is not None:
            self.stats.cached += 1
            classified = _apply_ai_classification(candidate, cached, self.ai_settings)
        else:
            return QueuedAiClassification(self, candidate)
        if (
            classified.ai_is_computer_part
            and not classified.ai_reject
            and classified.condition_status == "normal"
            and classified.ai_usable_for_market_price
            and (classified.ai_confidence or 0.0)
            >= float(self.ai_settings["confidence_threshold"])
        ):
            self.stats.accepted_normal += 1
        self.stats.classified_listings.append(classified)
        return classified

    def complete(
        self, candidate: Listing, attempt: ClassificationAttempt
    ) -> Listing:
        """Persist one worker result in the main thread and fail closed on errors."""
        assert self.classifier is not None
        self.stats.calls += 1
        self.stats.execution_seconds += attempt.execution_seconds
        self.database.record_ai_classification(
            candidate,
            classification_fingerprint(candidate),
            model=self.classifier.model,
            reasoning_effort=self.classifier.reasoning_effort,
            classifier_version=CLASSIFIER_VERSION,
            classification=attempt.classification,
            execution_duration_seconds=attempt.execution_seconds,
            error_reason=attempt.error,
        )
        if attempt.classification is None:
            self.stats.failures += 1
            failed = _failed_ai_listing(candidate, attempt.error or "Codex CLI classification failed")
            self.stats.classified_listings.append(failed)
            return failed
        classified = _apply_ai_classification(candidate, attempt.classification, self.ai_settings)
        if (
            classified.ai_is_computer_part
            and not classified.ai_reject
            and classified.condition_status == "normal"
            and classified.ai_usable_for_market_price
            and (classified.ai_confidence or 0.0)
            >= float(self.ai_settings["confidence_threshold"])
        ):
            self.stats.accepted_normal += 1
        self.stats.classified_listings.append(classified)
        return classified

    def __call__(self, candidate: Listing) -> Listing:
        """Sequential compatibility path used outside the concurrent live scan."""
        prepared = self.prepare(candidate)
        if isinstance(prepared, Listing):
            return prepared
        assert self.classifier is not None
        return self.complete(candidate, self.classifier.classify_attempt(candidate))


def _run_ai_task(work: QueuedAiClassification) -> tuple[ClassificationAttempt, str, str]:
    """Run the CLI outside SQLite's thread-bound connection."""
    assert work.processor.classifier is not None
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        attempt = work.processor.classifier.classify_attempt(work.listing)
    except Exception as exc:  # Defensive: an unexpected worker failure must fail closed.
        attempt = ClassificationAttempt(None, 0.0, str(exc))
    finished_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return attempt, started_at, finished_at


def run_ai_worker_pool(
    queued: Sequence[QueuedAiClassification],
    concurrency: int,
    on_complete: Callable[[QueuedAiClassification, Listing], None] | None = None,
    on_update: Callable[[AiPoolTelemetry], None] | None = None,
) -> AiPoolTelemetry:
    """Continuously refill a bounded AI worker pool as each task completes."""
    if concurrency < 1:
        raise ValueError("AI concurrency must be positive")
    telemetry = AiPoolTelemetry(initial_queue_length=len(queued), queue_length=len(queued))
    if not queued:
        return telemetry

    started = time.monotonic()
    iterator = iter(queued)
    futures: dict[Future[tuple[ClassificationAttempt, str, str]], QueuedAiClassification] = {}

    def submit_next(executor: ThreadPoolExecutor) -> bool:
        try:
            work = next(iterator)
        except StopIteration:
            return False
        futures[executor.submit(_run_ai_task, work)] = work
        telemetry.active_workers += 1
        telemetry.queue_length -= 1
        telemetry.max_concurrency_observed = max(
            telemetry.max_concurrency_observed, telemetry.active_workers
        )
        return True

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="codex-ai") as executor:
        while len(futures) < concurrency and submit_next(executor):
            pass
        while futures:
            completed, _ = wait(futures, timeout=0.5, return_when=FIRST_COMPLETED)
            if not completed:
                if on_update is not None:
                    on_update(telemetry)
                continue
            for future in completed:
                work = futures.pop(future)
                telemetry.active_workers -= 1
                try:
                    attempt, task_started, task_finished = future.result()
                except Exception as exc:  # Should be unreachable, but keeps all other tasks running.
                    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                    attempt, task_started, task_finished = ClassificationAttempt(None, 0.0, str(exc)), now, now
                # Refill before doing SQLite work so the newly free CLI slot is used immediately.
                submit_next(executor)
                result = work.processor.complete(work.listing, attempt)
                telemetry.completed_calls += 1
                telemetry.failures += int(attempt.classification is None)
                telemetry.subprocess_execution_seconds += attempt.execution_seconds
                telemetry.task_timings.append(
                    AiTaskTiming(
                        work.listing.product_id,
                        task_started,
                        task_finished,
                        attempt.execution_seconds,
                        attempt.classification is None,
                    )
                )
                if on_complete is not None:
                    on_complete(work, result)
                if on_update is not None:
                    on_update(telemetry)
    telemetry.wall_clock_seconds = time.monotonic() - started
    return telemetry


class StreamingAiWorkerPool:
    """A bounded pool that lets the crawler keep filling an AI review queue."""

    def __init__(
        self,
        concurrency: int,
        on_complete: Callable[[QueuedAiClassification, Listing], None],
        on_update: Callable[[AiPoolTelemetry], None] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("AI concurrency must be positive")
        self.concurrency = concurrency
        self.on_complete = on_complete
        self.on_update = on_update
        self.telemetry = AiPoolTelemetry()
        self._waiting: deque[QueuedAiClassification] = deque()
        self._futures: dict[Future[tuple[ClassificationAttempt, str, str]], QueuedAiClassification] = {}
        self._executor = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="codex-ai")
        self._closed = False

    def submit(self, work: QueuedAiClassification) -> None:
        self._waiting.append(work)
        self.telemetry.initial_queue_length += 1
        self._refill()
        self._publish()

    def _refill(self) -> None:
        while self._waiting and len(self._futures) < self.concurrency:
            work = self._waiting.popleft()
            self._futures[self._executor.submit(_run_ai_task, work)] = work
        self.telemetry.queue_length = len(self._waiting)
        self.telemetry.active_workers = len(self._futures)
        self.telemetry.max_concurrency_observed = max(
            self.telemetry.max_concurrency_observed, self.telemetry.active_workers
        )

    def _complete(self, future: Future[tuple[ClassificationAttempt, str, str]]) -> None:
        work = self._futures.pop(future)
        try:
            attempt, task_started, task_finished = future.result()
        except Exception as exc:
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            attempt, task_started, task_finished = ClassificationAttempt(None, 0.0, str(exc)), now, now
        result = work.processor.complete(work.listing, attempt)
        self.telemetry.completed_calls += 1
        self.telemetry.failures += int(attempt.classification is None)
        self.telemetry.subprocess_execution_seconds += attempt.execution_seconds
        self.telemetry.task_timings.append(
            AiTaskTiming(work.listing.product_id, task_started, task_finished,
                         attempt.execution_seconds, attempt.classification is None)
        )
        self.on_complete(work, result)

    def poll(self) -> None:
        for future in [future for future in self._futures if future.done()]:
            self._complete(future)
        self._refill()
        self._publish()

    def finish(self) -> AiPoolTelemetry:
        started = time.monotonic()
        while self._futures or self._waiting:
            if self._futures:
                done, _ = wait(self._futures, timeout=0.5, return_when=FIRST_COMPLETED)
                for future in done:
                    self._complete(future)
            self._refill()
            self._publish()
        self.telemetry.wall_clock_seconds += time.monotonic() - started
        self._executor.shutdown(wait=True)
        self._closed = True
        return self.telemetry

    def _publish(self) -> None:
        if self.on_update is not None:
            self.on_update(self.telemetry)


def build_ai_classifier(settings: dict[str, object]) -> CodexCliClassifier | None:
    if not settings.get("enabled", False):
        return None
    schema_path = Path(str(settings["schema_path"]))
    if not schema_path.is_absolute():
        schema_path = PROJECT_ROOT / schema_path
    return CodexCliClassifier(
        schema_path,
        command=str(settings["command"]),
        model=str(settings["model"]),
        reasoning_effort=str(settings["reasoning_effort"]),
        timeout_seconds=float(settings["timeout_seconds"]),
    )


def backfill_market_price(
    database: ListingDatabase,
    crawler: BunjangCrawler,
    product_name: str,
    settings: dict[str, object],
    *,
    sample_size: int = 40,
    classifier: CodexCliClassifier | None = None,
) -> MarketPriceBackfillResult:
    """Build one product's price evidence without invoking the alert scan.

    This intentionally does not use source watermarks or candidate state. It may
    revisit known listings, but preserves ``notified_at`` and never invokes the
    notifier.
    """
    if not 30 <= sample_size <= 50:
        raise ValueError("backfill sample_size must be from 30 to 50")
    target_name = normalize_product_name(product_name)
    if target_name != "Ryzen 5 5600X":
        raise ValueError("Backfill is currently limited to Ryzen 5 5600X")

    candidates: list[Listing] = []
    cursor: str | None = None
    while len(candidates) < sample_size:
        page = crawler.search_page(product_name, "market-price-backfill", cursor)
        candidates.extend(page.listings[: sample_size - len(candidates)])
        if not page.next_cursor:
            break
        cursor = page.next_cursor

    ai_settings = settings.get("ai_classification", {"enabled": False})
    processor = AiListingProcessor(database, classifier, dict(ai_settings), AiScanStats())
    excluded: list[BackfillExclusion] = []
    valid_standalone_listings = 0
    new_observations = 0

    def store_and_count(listing: Listing) -> None:
        nonlocal valid_standalone_listings, new_observations
        database.store_backfill_listing(listing)
        reason: str | None = None
        if listing.listing_status != "active":
            reason = f"status:{listing.listing_status}"
        elif listing.condition_status != "normal":
            reason = f"condition:{listing.condition_status}"
        elif listing.ai_scope != "standalone":
            reason = f"scope:{listing.ai_scope}"
        elif listing.ai_reject or listing.ai_is_computer_part is not True:
            reason = "ai_rejected"
        elif not listing.ai_usable_for_market_price:
            reason = "not_usable_for_market_price"
        elif (listing.ai_confidence or 0.0) < float(ai_settings["confidence_threshold"]):
            reason = "low_confidence"
        elif listing.ai_normalized_product_name != target_name:
            reason = "misclassified_product"
        if reason:
            excluded.append(BackfillExclusion(listing.product_id, listing.title, listing.price, reason))
            return
        valid_standalone_listings += 1
        if listing.product_id and not database.has_price_observation(
            listing.marketplace, listing.product_id, target_name
        ):
            if database.record_price_observation(
                listing,
                target_name,
                observed_at=listing.updated_at,
            ):
                new_observations += 1

    queued: list[QueuedAiClassification] = []
    for candidate in candidates:
        try:
            detailed = crawler.inspect(candidate)
        except Exception:
            excluded.append(
                BackfillExclusion(candidate.product_id, candidate.title, candidate.price, "detail_error")
            )
            continue
        prepared = processor.prepare(detailed)
        if isinstance(prepared, QueuedAiClassification):
            queued.append(prepared)
        else:
            store_and_count(prepared)

    run_ai_worker_pool(
        queued,
        int(ai_settings.get("ai_concurrency", 4)),
        lambda _work, listing: store_and_count(listing),
    )

    options = settings["market_price_estimation"]
    now = datetime.now(UTC)
    all_observations = database.price_observations(target_name)
    used = estimation_observations(
        all_observations, now=now, window_days=int(options["window_days"])
    )
    automatic_estimate = estimate_market_price(
        target_name,
        all_observations,
        manual_reference_price=load_market_prices().get(target_name),
        window_days=int(options["window_days"]),
        half_life_days=float(options["half_life_days"]),
        minimum_observations=int(options["minimum_observations"]),
        estimator=str(options["estimator"]),
        now=now,
    )
    weighted_median = estimate_market_price(
        target_name, used, manual_reference_price=None,
        window_days=int(options["window_days"]), half_life_days=float(options["half_life_days"]),
        minimum_observations=1, estimator="weighted_median", now=now,
    ).price if used else None
    weighted_mean = estimate_market_price(
        target_name, used, manual_reference_price=None,
        window_days=int(options["window_days"]), half_life_days=float(options["half_life_days"]),
        minimum_observations=1, estimator="weighted_mean", now=now,
    ).price if used else None

    def age_days(observation: PriceObservation) -> float:
        observed = datetime.fromisoformat(observation.observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=UTC)
        return max(0.0, (now - observed.astimezone(UTC)).total_seconds() / 86400)

    ages = [age_days(observation) for observation in used]
    return MarketPriceBackfillResult(
        target_name, len(candidates), valid_standalone_listings, new_observations,
        tuple(excluded), tuple(observation.observed_price for observation in used),
        max(ages) if ages else None, min(ages) if ages else None,
        automatic_estimate, weighted_median, weighted_mean,
    )


def print_market_price_backfill(result: MarketPriceBackfillResult) -> None:
    """Print an audit-friendly result for the isolated one-product backfill."""
    excluded_by_reason: dict[str, int] = {}
    for item in result.excluded:
        excluded_by_reason[item.reason] = excluded_by_reason.get(item.reason, 0) + 1
    estimate = result.automatic_estimate
    automatic_price = estimate.price if estimate.automatic else None
    oldest_age = f"{result.oldest_age_days:.2f}" if result.oldest_age_days is not None else "-"
    newest_age = f"{result.newest_age_days:.2f}" if result.newest_age_days is not None else "-"
    print(
        f"BACKFILL | product={result.product_name} | inspected={result.search_results_inspected} | "
        f"valid_standalone={result.valid_standalone_listings} | new_observations={result.new_observations} | "
        f"excluded={len(result.excluded)} | passes_minimum={estimate.automatic}"
    )
    print("BACKFILL_EXCLUDED | " + ", ".join(
        f"{reason}={count}" for reason, count in sorted(excluded_by_reason.items())
    ))
    print("BACKFILL_PRICES_USED | " + ", ".join(f"{price:,}" for price in result.prices_used))
    print(
        f"BACKFILL_ESTIMATE | automatic={automatic_price if automatic_price is not None else '-'} | "
        f"weighted_median={result.weighted_median if result.weighted_median is not None else '-'} | "
        f"weighted_mean={result.weighted_mean if result.weighted_mean is not None else '-'} | "
        f"oldest_age_days={oldest_age}"
    )
    print(
        f"BACKFILL_AGE | oldest_days={oldest_age} | newest_days={newest_age} | "
        f"passes_minimum={estimate.automatic}"
    )
    for item in result.excluded[:8]:
        print(
            f"BACKFILL_EXAMPLE | reason={item.reason} | product_id={item.product_id or '-'} | "
            f"price={item.price} | title={item.title}"
        )


def track_sale_statuses(
    database: ListingDatabase,
    crawler: BunjangCrawler,
    tracking_settings: dict[str, object],
    *,
    limit: int | None = None,
) -> SaleStatusTrackingResult:
    """Recheck only due, already-qualified active listings; never invoke AI or email."""
    started = time.monotonic()
    candidates = database.sale_status_candidates(tracking_settings, limit=limit)
    checked = sold_transitions = failures = 0
    for candidate in candidates:
        try:
            detailed = crawler.inspect(candidate.listing)
            sold_transitions += int(database.record_sale_status_check(detailed))
            checked += 1
        except Exception:
            failures += 1
    return SaleStatusTrackingResult(
        len(candidates), checked, sold_transitions, failures, time.monotonic() - started
    )


def _backfill_exclusion_reason(listing: Listing, confidence_threshold: float = 0.85) -> str:
    if listing.listing_status != "active":
        return f"status:{listing.listing_status}"
    if listing.condition_status != "normal":
        return f"condition:{listing.condition_status}"
    if listing.ai_scope != "standalone":
        return f"scope:{listing.ai_scope}"
    if listing.ai_reject or listing.ai_is_computer_part is not True:
        return "ai_rejected"
    if not listing.ai_usable_for_market_price:
        return "not_usable_for_market_price"
    if (listing.ai_confidence or 0.0) < confidence_threshold:
        return "low_confidence"
    if not listing.ai_normalized_product_name:
        return "model_mismatched"
    return ""


def _print_backfill_detail_retry_statistics(
    database: ListingDatabase,
    checkpoint_key: str,
    source_key: str,
    progress: Callable[[str], None],
) -> None:
    """Emit a compact, query-scoped retry breakdown for operators."""
    rows = database.backfill_detail_retry_statistics(checkpoint_key)
    categories = ",".join(
        f"{row['error_category']}:queued={row['queued_count']},terminal={row['terminal_count']}"
        for row in rows
    ) or "none"
    progress(f"BACKFILL_DETAIL_RETRY_STATS | key={source_key} | categories={categories}")


def run_full_market_price_backfill(
    database: ListingDatabase,
    crawler: BunjangCrawler,
    sources: Sequence[dict[str, object]],
    settings: dict[str, object],
    classifier: CodexCliClassifier | None,
    *,
    progress: Callable[[str], None] = print,
) -> FullBackfillResult:
    """Resume every configured query through its real cursor end, without alerts."""
    started = time.monotonic()
    manual_prices = load_market_prices()
    ai_settings = dict(settings.get("ai_classification", {"enabled": False}))
    total_inspected = total_valid = total_excluded = total_ai_calls = total_ai_failures = 0
    max_concurrency = completed_queries = incomplete_queries = 0
    total_pages_scanned = 0
    ai_concurrency = int(ai_settings.get("ai_concurrency", 4))
    reporter = BackfillProgressReporter(
        len(sources), emit=progress,
        interval_seconds=float(ai_settings.get("progress_interval_seconds", 10.0)),
    )

    def report_live(
        crawl_status: str,
        telemetry: AiPoolTelemetry | None = None,
        *,
        crawl_complete: bool = False,
        force: bool = False,
    ) -> None:
        pool = telemetry or AiPoolTelemetry()
        reporter.report(
            completed_queries=completed_queries,
            pages_scanned=total_pages_scanned,
            listings_inspected=total_inspected,
            valid_observations=total_valid,
            excluded_listings=total_excluded,
            ai_queue_size=pool.queue_length,
            active_ai_workers=pool.active_workers,
            ai_completed=total_ai_calls + pool.completed_calls,
            ai_failures=total_ai_failures + pool.failures,
            concurrency=ai_concurrency,
            crawl_status=crawl_status,
            crawl_complete=crawl_complete,
            force=force,
        )

    report_live("starting", force=True)

    for source in sources:
        source_key = str(source["key"])
        query = str(source["query"])
        checkpoint_key = f"all-market-price-backfill:{source_key}"
        checkpoint = database.backfill_checkpoint(checkpoint_key)
        cursor = str(checkpoint["cursor"]) if checkpoint and checkpoint["cursor"] else None
        pages = int(checkpoint["pages_scanned"]) if checkpoint else 0
        unique_found = int(checkpoint["unique_listings_found"]) if checkpoint else 0
        valid_observations = int(checkpoint["valid_observations"]) if checkpoint else 0
        excluded = int(checkpoint["excluded_listings"]) if checkpoint else 0
        ai_calls = int(checkpoint["ai_calls"]) if checkpoint else 0
        ai_failures = int(checkpoint["ai_failures"]) if checkpoint else 0
        query_completed = False

        def persist(listing: Listing) -> None:
            nonlocal valid_observations, excluded
            database.store_backfill_listing(listing)
            reason = _backfill_exclusion_reason(
                listing, float(ai_settings.get("confidence_threshold", 0.85))
            )
            if reason:
                excluded += 1
            else:
                normalized_name = listing.ai_normalized_product_name
                assert normalized_name is not None
                if not database.has_price_observation(
                    listing.marketplace, listing.product_id or "", normalized_name
                ) and database.record_price_observation(listing, normalized_name):
                    valid_observations += 1
            if listing.product_id:
                database.mark_backfill_product_seen(
                    listing.marketplace, listing.product_id, checkpoint_key
                )
                database.clear_backfill_detail_retry(listing.marketplace, listing.product_id)

        def persist_detail_failure(item: Listing, exc: Exception) -> None:
            transient = database.queue_backfill_detail_retry(checkpoint_key, item, exc)
            http_status, exception_type, category, retry_count, _message = detail_error_diagnostics(exc)
            if transient:
                status = "queued"
            else:
                # A confirmed missing/deleted listing, or another permanent 4xx,
                # must not keep this cursor item alive forever.
                database.mark_backfill_listing_unavailable(item)
                if item.product_id:
                    database.mark_backfill_product_seen(
                        item.marketplace, item.product_id, checkpoint_key
                    )
                status = "terminal_unavailable"
            progress(
                f"BACKFILL_DETAIL_RETRY | key={source_key} | product_id={item.product_id} | "
                f"status={status} | http_status={http_status if http_status is not None else '-'} | "
                f"exception_type={exception_type} | category={category} | "
                f"request_retry_count={retry_count}"
            )

        # Retry previously exhausted detail fetches before considering the cursor
        # complete. A failure remains queued, but must never make the query pause.
        retry_valid_before, retry_excluded_before = valid_observations, excluded
        retry_processor = AiListingProcessor(database, classifier, ai_settings, AiScanStats())
        retry_queued: list[QueuedAiClassification] = []
        for item in database.backfill_detail_retries(checkpoint_key):
            try:
                detailed = crawler.inspect(item)
            except Exception as exc:
                persist_detail_failure(item, exc)
                continue
            prepared = retry_processor.prepare(detailed)
            if isinstance(prepared, QueuedAiClassification):
                retry_queued.append(prepared)
            else:
                persist(prepared)
        retry_timing_count = 0

        def report_retry_pool(pool: AiPoolTelemetry) -> None:
            nonlocal retry_timing_count
            for timing in pool.task_timings[retry_timing_count:]:
                reporter.add_ai_duration(timing.execution_seconds)
            retry_timing_count = len(pool.task_timings)
            report_live("retrying_details", pool)

        retry_telemetry = run_ai_worker_pool(
            retry_queued, ai_concurrency, lambda _work, listing: persist(listing), report_retry_pool,
        )
        ai_calls += retry_telemetry.completed_calls
        ai_failures += retry_telemetry.failures
        total_ai_calls += retry_telemetry.completed_calls
        total_ai_failures += retry_telemetry.failures
        max_concurrency = max(max_concurrency, retry_telemetry.max_concurrency_observed)
        total_valid += valid_observations - retry_valid_before
        total_excluded += excluded - retry_excluded_before
        _print_backfill_detail_retry_statistics(database, checkpoint_key, source_key, progress)

        if checkpoint is not None and bool(checkpoint["completed"]):
            database.update_backfill_checkpoint(
                checkpoint_key, query, cursor=cursor, pages_scanned=pages,
                unique_listings_found=unique_found, valid_observations=valid_observations,
                excluded_listings=excluded, ai_calls=ai_calls, ai_failures=ai_failures,
                completed=True,
            )
            progress(f"BACKFILL_QUERY | key={source_key} | status=already_complete")
            completed_queries += 1
            report_live("query_already_complete", force=True)
            continue

        query_valid_before, query_excluded_before = valid_observations, excluded
        stream_timing_count = 0

        def report_stream_pool(pool: AiPoolTelemetry) -> None:
            nonlocal stream_timing_count
            for timing in pool.task_timings[stream_timing_count:]:
                reporter.add_ai_duration(timing.execution_seconds)
            stream_timing_count = len(pool.task_timings)
            report_live("crawling", pool)

        stream_pool = StreamingAiWorkerPool(
            ai_concurrency, lambda _work, listing: persist(listing), report_stream_pool
        )

        while True:
            try:
                page = crawler.search_page(query, checkpoint_key, cursor)
            except Exception as exc:
                telemetry = stream_pool.finish()
                ai_calls += telemetry.completed_calls
                ai_failures += telemetry.failures
                total_ai_calls += telemetry.completed_calls
                total_ai_failures += telemetry.failures
                max_concurrency = max(max_concurrency, telemetry.max_concurrency_observed)
                total_valid += valid_observations - query_valid_before
                total_excluded += excluded - query_excluded_before
                database.update_backfill_checkpoint(
                    checkpoint_key, query, cursor=cursor, pages_scanned=pages,
                    unique_listings_found=unique_found, valid_observations=valid_observations,
                    excluded_listings=excluded, ai_calls=ai_calls, ai_failures=ai_failures,
                    completed=False,
                )
                progress(f"BACKFILL_QUERY | key={source_key} | status=paused | error={type(exc).__name__}")
                report_live("crawl_paused", force=True)
                break

            page_seen: set[str] = set()
            candidates: list[Listing] = []
            for item in page.listings:
                if not item.product_id or item.product_id in page_seen:
                    continue
                page_seen.add(item.product_id)
                if (
                    database.is_backfill_product_seen(item.marketplace, item.product_id)
                    or database.is_backfill_detail_retry_queued(item.marketplace, item.product_id)
                ):
                    continue
                candidates.append(item)
            unique_found += len(candidates)
            total_inspected += len(candidates)
            processor = AiListingProcessor(database, classifier, ai_settings, AiScanStats())

            for item in candidates:
                try:
                    detailed = crawler.inspect(item)
                except Exception as exc:
                    # BunjangCrawler has already retried transient request failures.
                    # Preserve only transient failures for a later pass; terminal
                    # failures are durably audited and marked unavailable.
                    persist_detail_failure(item, exc)
                    continue
                prepared = processor.prepare(detailed)
                if isinstance(prepared, QueuedAiClassification):
                    stream_pool.submit(prepared)
                else:
                    persist(prepared)
                # Detail HTTP failures never reach this pool; completed reviews
                # are persisted while the crawler continues to later candidates.
                stream_pool.poll()
            pages += 1
            total_pages_scanned += 1
            cursor = page.next_cursor
            query_completed = cursor is None
            database.update_backfill_checkpoint(
                checkpoint_key, query, cursor=cursor, pages_scanned=pages,
                unique_listings_found=unique_found, valid_observations=valid_observations,
                excluded_listings=excluded, ai_calls=ai_calls, ai_failures=ai_failures,
                completed=False,
            )
            target_name = normalize_product_name(query)
            estimate = (
                market_price_estimate(database, target_name, manual_prices, settings)
                if target_name else None
            )
            price = f"{estimate.price:,}" if estimate and estimate.price is not None else "-"
            progress(
                f"BACKFILL_PROGRESS | key={source_key} | pages={pages} | unique={unique_found} | "
                f"valid_observations={valid_observations} | excluded={excluded} | "
                f"ai_calls={ai_calls} | ai_failures={ai_failures} | market_price={price}"
            )
            _print_backfill_detail_retry_statistics(database, checkpoint_key, source_key, progress)
            if query_completed:
                report_live("crawl_complete_draining_ai", stream_pool.telemetry,
                            crawl_complete=True, force=True)
                telemetry = stream_pool.finish()
                ai_calls += telemetry.completed_calls
                ai_failures += telemetry.failures
                total_ai_calls += telemetry.completed_calls
                total_ai_failures += telemetry.failures
                max_concurrency = max(max_concurrency, telemetry.max_concurrency_observed)
                total_valid += valid_observations - query_valid_before
                total_excluded += excluded - query_excluded_before
                database.update_backfill_checkpoint(
                    checkpoint_key, query, cursor=cursor, pages_scanned=pages,
                    unique_listings_found=unique_found, valid_observations=valid_observations,
                    excluded_listings=excluded, ai_calls=ai_calls, ai_failures=ai_failures,
                    completed=True,
                )
                completed_queries += 1
                report_live("query_complete", crawl_complete=True, force=True)
                break
        if not query_completed:
            incomplete_queries += 1

    report_live("complete", crawl_complete=True, force=True)

    return FullBackfillResult(
        total_inspected, total_valid, total_excluded, completed_queries, incomplete_queries,
        total_ai_calls, total_ai_failures, max_concurrency, time.monotonic() - started,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sample", action="store_true", help="run the local sample")
    mode.add_argument("--live", action="store_true", help="scan configured Bunjang sources")
    mode.add_argument(
        "--track-sale-status", action="store_true",
        help="recheck only due stored active standalone listings; never sends email",
    )
    mode.add_argument(
        "--market-price-backfill", metavar="PRODUCT",
        help="backfill one approved product's market-price observations without alerts",
    )
    mode.add_argument(
        "--full-market-price-backfill", action="store_true",
        help="resume every configured market-price query through its cursor end; never sends email",
    )
    mode.add_argument("--market-price", metavar="PRODUCT", help="show one product's market-price estimate")
    mode.add_argument("--setup-email", action="store_true", help="save Gmail app password with hidden input")
    mode.add_argument("--test-email", action="store_true", help="send one bargain-format SMTP test email")
    parser.add_argument("--limit", type=int, default=10, help="maximum candidates per Bunjang query")
    parser.add_argument("--source", help="scan only one configured Bunjang source key")
    parser.add_argument("--no-email", action="store_true", help="disable email delivery for this scan")
    parser.add_argument(
        "--backfill-limit", type=int, default=40,
        help="recent search results to inspect in backfill mode (30-50; default: 40)",
    )
    parser.add_argument(
        "--status-limit", type=int, default=100,
        help="maximum due listings to detail-check in sale-status mode (default: 100)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.setup_email:
        setup_smtp_password()
        load_smtp_password()
        return 0
    load_smtp_password()
    if args.test_email:
        settings = load_settings()
        notifier = EmailNotifier.from_settings(settings["email_notifications"])
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            deals = test_email_deals(database, load_market_prices(), settings)
            notifier.send_digest(
                deals,
                {deal.normalized_name: "test" for deal in deals},
            )
        return 0
    if args.market_price:
        settings = load_settings()
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            estimate = market_price_estimate(
                database, args.market_price, load_market_prices(), settings
            )
        print_market_price_debug(estimate)
        return 0
    if args.market_price_backfill:
        settings = load_settings()
        crawler = BunjangCrawler(
            settings["request_delay_seconds"],
            settings.get("request_timeout_seconds", 10),
            settings.get("user_agent", "used_pc_finder/0.1"),
            # Backfill establishes a price distribution, not alert eligibility.
            maximum_listing_price=None,
        )
        classifier = build_ai_classifier(settings.get("ai_classification", {"enabled": False}))
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            result = backfill_market_price(
                database,
                crawler,
                args.market_price_backfill,
                settings,
                sample_size=args.backfill_limit,
                classifier=classifier,
            )
        print_market_price_backfill(result)
        return 0
    if args.full_market_price_backfill:
        settings = load_settings()
        crawler = BunjangCrawler(
            settings["request_delay_seconds"],
            settings.get("request_timeout_seconds", 10),
            settings.get("user_agent", "used_pc_finder/0.1"),
            maximum_listing_price=None,
        )
        classifier = build_ai_classifier(settings.get("ai_classification", {"enabled": False}))
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            result = run_full_market_price_backfill(
                database, crawler, list(settings["bunjang_sources"]), settings, classifier
            )
            target_names = {
                name for source in settings["bunjang_sources"]
                if (name := normalize_product_name(str(source["query"]))) is not None
            }
            estimates = [
                market_price_estimate(database, name, load_market_prices(), settings)
                for name in sorted(target_names)
            ]
        print(
            f"BACKFILL_FINAL | inspected={result.listings_inspected} | "
            f"valid_observations={result.valid_observations_collected} | excluded={result.excluded_listings} | "
            f"completed_queries={result.completed_queries} | incomplete_queries={result.incomplete_queries} | "
            f"automatic_prices={sum(estimate.automatic for estimate in estimates)} | "
            f"manual_fallbacks={sum(not estimate.automatic for estimate in estimates)} | "
            f"ai_calls={result.ai_calls} | ai_failures={result.ai_failures} | "
            f"max_concurrency={result.max_concurrency_observed} | elapsed_seconds={result.elapsed_seconds:.3f}"
        )
        return 0
    if args.track_sale_status:
        settings = load_settings()
        crawler = BunjangCrawler(
            settings["request_delay_seconds"],
            settings.get("request_timeout_seconds", 10),
            settings.get("user_agent", "used_pc_finder/0.1"),
            maximum_listing_price=None,
        )
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            result = track_sale_statuses(
                database,
                crawler,
                dict(settings["sale_status_tracking"]),
                limit=args.status_limit,
            )
        print(
            f"SALE_STATUS | due={result.due_listings} | checked={result.active_listings_checked} | "
            f"sold_transitions={result.sold_transitions} | request_failures={result.request_failures} | "
            f"ai_candidates=0 | ai_calls=0 | ai_failures=0 | max_concurrency=0 | "
            f"wall_clock_seconds={result.elapsed_seconds:.3f}"
        )
        return 0
    if args.live:
        settings = load_settings()
        crawler = BunjangCrawler(
            settings["request_delay_seconds"],
            settings.get("request_timeout_seconds", 10),
            settings.get("user_agent", "used_pc_finder/0.1"),
            int(settings["maximum_listing_price"]),
        )
        ai_settings = settings.get("ai_classification", {"enabled": False})
        classifier = build_ai_classifier(ai_settings)
        listings: list[Listing] = []
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            seen_product_ids: set[str] = set()
            scan_results = []
            scan_stats = []
            queued_ai: list[QueuedAiClassification] = []
            sources = settings.get("bunjang_sources", [])
            if args.source:
                sources = [source for source in sources if source["key"] == args.source]
                if not sources:
                    parser.error(f"unknown Bunjang source key: {args.source}")
            for source in sources:
                ai_stats = AiScanStats()
                processor = AiListingProcessor(
                    database, classifier, ai_settings, ai_stats
                )

                def prepare_for_queue(candidate: Listing) -> Listing:
                    prepared = processor.prepare(candidate)
                    if isinstance(prepared, QueuedAiClassification):
                        queued_ai.append(prepared)
                        return candidate
                    return prepared

                result = scan_bunjang_source(
                    crawler,
                    database,
                    source,
                    prepare_for_queue,
                    seen_product_ids,
                    record_limit=args.limit,
                )
                scan_results.append(result)
                scan_stats.append(ai_stats)

            queued_locations: dict[str, tuple[int, int]] = {}
            for result_index, result in enumerate(scan_results):
                for listing_index, item in enumerate(result.listings):
                    if item.product_id and any(
                        work.listing.product_id == item.product_id for work in queued_ai
                    ):
                        queued_locations[item.product_id] = (result_index, listing_index)
            observation_additions = [0 for _result in scan_results]

            def save_completed_ai(work: QueuedAiClassification, classified: Listing) -> None:
                if not classified.product_id:
                    return
                result_index, listing_index = queued_locations[classified.product_id]
                result = scan_results[result_index]
                original_state = result.processed_states[classified.product_id]
                database.store_processed(classified, database.candidate_state(classified))
                result.listings[listing_index] = classified
                normalized_name = comparable_product_name(classified, require_ai=True)
                should_observe = (
                    normalized_name is not None
                    and (
                        original_state.status == "new"
                        or original_state.previous_price != classified.price
                        or not database.has_price_observation(
                            classified.marketplace, classified.product_id, normalized_name
                        )
                    )
                )
                if should_observe and database.record_price_observation(classified, normalized_name):
                    observation_additions[result_index] += 1

            pool_telemetry = run_ai_worker_pool(
                queued_ai,
                int(ai_settings["ai_concurrency"]),
                save_completed_ai,
            )
            pool_telemetry.cache_hits = sum(item.cached for item in scan_stats)
            scan_results = [
                replace(
                    result,
                    price_observations_recorded=(
                        result.price_observations_recorded + observation_additions[index]
                    ),
                )
                for index, result in enumerate(scan_results)
            ]
            listings = [item for result in scan_results for item in result.listings]
            for source, result, ai_stats in zip(sources, scan_results, scan_stats, strict=True):
                pricing_candidates = sum(
                    comparable_product_name(item, require_ai=classifier is not None) is not None
                    for item in result.listings
                )
                print(
                    f"SOURCE | bunjang:{source['key']} | records_fetched={result.search_records_fetched} | "
                    f"new={result.new_count} | updated={result.updated_count} | "
                    f"pending_ai={result.pending_ai_count} | unchanged={result.unchanged_count} | "
                    f"duplicates={result.duplicate_count} | detail_requests={result.detail_requests} | "
                    f"pages={result.pages_fetched} | over_budget={result.over_budget_count} | "
                    f"irrelevant={result.irrelevant_count} | "
                    f"price_observations={result.price_observations_recorded} | "
                    f"deterministic_rejects={ai_stats.deterministic_rejects} | "
                    f"ai_candidates={ai_stats.candidates} | ai_calls={ai_stats.calls} | "
                    f"ai_cached={ai_stats.cached} | ai_failures={ai_stats.failures} | "
                    f"ai_accepted_normal={ai_stats.accepted_normal} | "
                    f"ai_seconds={ai_stats.execution_seconds:.3f} | "
                    f"pricing_candidates={pricing_candidates} | monotonic={result.ordering_monotonic} | "
                    f"stopped_at_watermark={result.stopped_at_watermark}"
                )
                for item in result.listings:
                    print(
                        f"FOUND | {item.title} | {item.price:,} KRW | "
                        f"{item.condition_status} | {item.url}"
                    )
            manual_prices = load_market_prices()
            price_estimates = effective_market_price_estimates(
                listings, database, manual_prices, settings, require_ai=classifier is not None
            )
            deals = find_deals(
                listings,
                {name: estimate.price for name, estimate in price_estimates.items()},
                settings["minimum_discount_percent"],
                require_ai=classifier is not None,
            )
            pricing_sources = {
                name: "automatic" if estimate.automatic else "manual"
                for name, estimate in price_estimates.items()
            }
            for deal in deals:
                print(f"DEAL | {format_deal(deal)}")
            for normalized_name in sorted({
                name for item in listings if (name := comparable_product_name(
                    item, require_ai=classifier is not None
                ))
            }):
                print_market_price_debug(
                    market_price_estimate(database, normalized_name, manual_prices, settings)
                )
            for ai_stats in scan_stats:
                for item in ai_stats.classified_listings:
                    print(
                        format_ai_listing_audit(
                            item,
                            database,
                            manual_prices,
                            settings,
                            float(settings["minimum_discount_percent"]),
                        )
                    )
            print(
                f"AI_POOL | initial_queue_length={pool_telemetry.initial_queue_length} | "
                f"queue_length={pool_telemetry.queue_length} | "
                f"active_workers={pool_telemetry.active_workers} | "
                f"max_concurrency_observed={pool_telemetry.max_concurrency_observed} | "
                f"completed_calls={pool_telemetry.completed_calls} | "
                f"failures={pool_telemetry.failures} | cache_hits={pool_telemetry.cache_hits} | "
                f"subprocess_seconds={pool_telemetry.subprocess_execution_seconds:.3f} | "
                f"wall_clock_seconds={pool_telemetry.wall_clock_seconds:.3f}"
            )
            print(
                f"NETWORK | bunjang_retries={crawler.request_retries} | "
                f"bunjang_permanent_failures={crawler.permanent_failures} | "
                f"bunjang_failed_requests={crawler.request_failures}"
            )
            for task in pool_telemetry.task_timings:
                print(
                    f"AI_TASK | product_id={task.product_id or '-'} | started_at={task.started_at} | "
                    f"finished_at={task.finished_at} | execution_seconds={task.execution_seconds:.3f} | "
                    f"failed={task.failed}"
                )
            email_settings = settings.get("email_notifications", {"enabled": False})
            if args.no_email:
                email_settings = dict(email_settings, enabled=False)
            simulated_notifications = sum(
                not database.was_notified(deal.listing) for deal in deals
            )
            sent = send_unnotified_deal_digest(
                deals, database, email_settings, pricing_sources
            )
            if sent:
                print(f"EMAIL | Sent one digest containing {sent} deal(s)")
            total_ai_calls = sum(item.calls for item in scan_stats)
            total_ai_seconds = sum(item.execution_seconds for item in scan_stats)
            total_pricing_candidates = sum(
                comparable_product_name(item, require_ai=classifier is not None) is not None
                for item in listings
            )
            print(
                f"PIPELINE | search_records={sum(item.search_records_fetched for item in scan_results)} | "
                f"budget_filtered={sum(item.over_budget_count for item in scan_results)} | "
                f"irrelevant_filtered={sum(item.irrelevant_count for item in scan_results)} | "
                f"duplicates={sum(item.duplicate_count for item in scan_results)} | "
                f"new={sum(item.new_count for item in scan_results)} | "
                f"updated={sum(item.updated_count for item in scan_results)} | "
                f"pending_ai={sum(item.pending_ai_count for item in scan_results)} | "
                f"unchanged={sum(item.unchanged_count for item in scan_results)} | "
                f"detail_requests={sum(item.detail_requests for item in scan_results)} | "
                f"deterministic_rejects={sum(item.deterministic_rejects for item in scan_stats)} | "
                f"ai_candidates={sum(item.candidates for item in scan_stats)} | ai_calls={total_ai_calls} | "
                f"ai_cache_hits={pool_telemetry.cache_hits} | "
                f"ai_failures={sum(item.failures for item in scan_stats)} | "
                f"ai_accepted_normal={sum(item.accepted_normal for item in scan_stats)} | "
                f"pricing_candidates={total_pricing_candidates} | "
                f"simulated_notifications={simulated_notifications} | emails_sent={sent} | "
                f"ai_total_seconds={total_ai_seconds:.3f} | "
                f"ai_average_seconds={(total_ai_seconds / total_ai_calls) if total_ai_calls else 0:.3f} | "
                f"ai_wall_clock_seconds={pool_telemetry.wall_clock_seconds:.3f} | "
                f"ai_max_concurrency={pool_telemetry.max_concurrency_observed}"
            )
        if not settings.get("bunjang_sources"):
            print("No Bunjang sources configured in config/settings.json.")
        return 0
    for deal in sample_deals():
        print(format_deal(deal))
    return 0
