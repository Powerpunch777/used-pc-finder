"""Application orchestration and text output."""

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from .ai_classifier import (
    CLASSIFIER_VERSION,
    AIClassification,
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
from .bunjang import BunjangCrawler
from .bunjang_scan import scan_bunjang_source
from .database import ListingDatabase
from .market_estimator import MarketPriceEstimate, estimate_market_price
from .models import Deal, Listing
from .notifications import send_unnotified_deals
from .pre_ai_filter import cheap_rejection_reason
from .pricing import comparable_product_name, find_deals
from .parser import normalize_product_name


@dataclass(slots=True)
class AiScanStats:
    deterministic_rejects: int = 0
    candidates: int = 0
    calls: int = 0
    failures: int = 0
    accepted_normal: int = 0
    cached: int = 0
    deferred: int = 0
    execution_seconds: float = 0.0


@dataclass(slots=True)
class AiCallBudget:
    calls: int = 0


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
    )
    return find_deals(
        [sample], load_market_prices(), settings["minimum_discount_percent"]
    )


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
        )
    if classifier is None:
        return listing
    if hasattr(classifier, "classify_attempt"):
        attempt = classifier.classify_attempt(listing)
        result = attempt.classification
        error = attempt.error
    else:
        result = classifier.classify(listing)
        error = None
    if result is None:
        return replace(
            listing,
            condition_status="unknown",
            ai_is_computer_part=False,
            ai_confidence=0.0,
            ai_reject=True,
            ai_reason=error or "Codex CLI classification failed",
        )
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
        or result.condition_status != "normal"
        or result.confidence < minimum_confidence
        or not canonical_name
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
    )


class AiListingProcessor:
    """Apply deterministic gates, cache reuse, and bounded authenticated CLI calls."""

    def __init__(
        self,
        database: ListingDatabase,
        classifier: CodexCliClassifier | None,
        ai_settings: dict[str, object],
        stats: AiScanStats,
        call_budget: AiCallBudget | None = None,
    ):
        self.database = database
        self.classifier = classifier
        self.ai_settings = ai_settings
        self.stats = stats
        self.call_budget = call_budget or AiCallBudget()

    def __call__(self, candidate: Listing) -> Listing:
        rejection_reason = cheap_rejection_reason(candidate)
        if rejection_reason:
            self.stats.deterministic_rejects += 1
            return replace(
                candidate,
                ai_is_computer_part=False,
                ai_confidence=0.0,
                ai_reject=True,
                ai_reason=rejection_reason,
            )
        if self.classifier is None:
            return candidate
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
        elif self.call_budget.calls >= int(self.ai_settings["max_ai_calls_per_scan"]):
            self.stats.deferred += 1
            return candidate
        else:
            attempt = self.classifier.classify_attempt(candidate)
            self.stats.calls += 1
            self.call_budget.calls += 1
            self.stats.execution_seconds += attempt.execution_seconds
            self.database.record_ai_classification(
                candidate,
                fingerprint,
                model=self.classifier.model,
                reasoning_effort=self.classifier.reasoning_effort,
                classifier_version=CLASSIFIER_VERSION,
                classification=attempt.classification,
                execution_duration_seconds=attempt.execution_seconds,
                error_reason=attempt.error,
            )
            if attempt.classification is None:
                self.stats.failures += 1
                return replace(
                    candidate,
                    condition_status="unknown",
                    ai_is_computer_part=False,
                    ai_confidence=0.0,
                    ai_reject=True,
                    ai_reason=attempt.error or "Codex CLI classification failed",
                )
            classified = _apply_ai_classification(
                candidate, attempt.classification, self.ai_settings
            )
        if (
            classified.ai_is_computer_part
            and not classified.ai_reject
            and classified.condition_status == "normal"
            and (classified.ai_confidence or 0.0)
            >= float(self.ai_settings["confidence_threshold"])
        ):
            self.stats.accepted_normal += 1
        return classified


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sample", action="store_true", help="run the local sample")
    mode.add_argument("--live", action="store_true", help="scan configured Bunjang sources")
    mode.add_argument("--market-price", metavar="PRODUCT", help="show one product's market-price estimate")
    parser.add_argument("--limit", type=int, default=10, help="maximum candidates per Bunjang query")
    parser.add_argument("--source", help="scan only one configured Bunjang source key")
    parser.add_argument("--no-email", action="store_true", help="disable email delivery for this scan")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.market_price:
        settings = load_settings()
        with ListingDatabase(DEFAULT_DATABASE_PATH) as database:
            database.initialize()
            estimate = market_price_estimate(
                database, args.market_price, load_market_prices(), settings
            )
        print_market_price_debug(estimate)
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
            ai_call_budget = AiCallBudget()
            sources = settings.get("bunjang_sources", [])
            if args.source:
                sources = [source for source in sources if source["key"] == args.source]
                if not sources:
                    parser.error(f"unknown Bunjang source key: {args.source}")
            for source in sources:
                ai_stats = AiScanStats()
                processor = AiListingProcessor(
                    database, classifier, ai_settings, ai_stats, ai_call_budget
                )

                result = scan_bunjang_source(
                    crawler,
                    database,
                    source,
                    processor,
                    seen_product_ids,
                    record_limit=args.limit,
                )
                pricing_candidates = sum(
                    comparable_product_name(item, require_ai=classifier is not None) is not None
                    for item in result.listings
                )
                print(
                    f"SOURCE | bunjang:{source['key']} | records_fetched={result.search_records_fetched} | "
                    f"new={result.new_count} | updated={result.updated_count} | "
                    f"unchanged={result.unchanged_count} | duplicates={result.duplicate_count} | "
                    f"detail_requests={result.detail_requests} | pages={result.pages_fetched} | "
                    f"over_budget={result.over_budget_count} | "
                    f"price_observations={result.price_observations_recorded} | "
                    f"deterministic_rejects={ai_stats.deterministic_rejects} | "
                    f"ai_candidates={ai_stats.candidates} | ai_calls={ai_stats.calls} | "
                    f"ai_cached={ai_stats.cached} | ai_deferred={ai_stats.deferred} | "
                    f"ai_failures={ai_stats.failures} | ai_accepted_normal={ai_stats.accepted_normal} | "
                    f"ai_seconds={ai_stats.execution_seconds:.3f} | "
                    f"pricing_candidates={pricing_candidates} | "
                    f"monotonic={result.ordering_monotonic} | "
                    f"stopped_at_watermark={result.stopped_at_watermark}"
                )
                for item in result.listings:
                    print(
                        f"FOUND | {item.title} | {item.price:,} KRW | "
                        f"{item.condition_status} | {item.url}"
                    )
                listings.extend(result.listings)
                scan_results.append(result)
                scan_stats.append(ai_stats)
            manual_prices = load_market_prices()
            deals = find_deals(
                listings,
                effective_market_prices(
                    listings, database, manual_prices, settings, require_ai=classifier is not None
                ),
                settings["minimum_discount_percent"],
                require_ai=classifier is not None,
            )
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
            email_settings = settings.get("email_notifications", {"enabled": False})
            if args.no_email:
                email_settings = dict(email_settings, enabled=False)
            sent = send_unnotified_deals(deals, database, email_settings)
            if sent:
                print(f"EMAIL | Sent {sent} deal notification(s)")
            total_ai_calls = sum(item.calls for item in scan_stats)
            total_ai_seconds = sum(item.execution_seconds for item in scan_stats)
            total_pricing_candidates = sum(
                comparable_product_name(item, require_ai=classifier is not None) is not None
                for item in listings
            )
            print(
                f"PIPELINE | search_records={sum(item.search_records_fetched for item in scan_results)} | "
                f"budget_filtered={sum(item.over_budget_count for item in scan_results)} | "
                f"unchanged={sum(item.unchanged_count for item in scan_results)} | "
                f"deterministic_rejects={sum(item.deterministic_rejects for item in scan_stats)} | "
                f"ai_candidates={sum(item.candidates for item in scan_stats)} | ai_calls={total_ai_calls} | "
                f"ai_failures={sum(item.failures for item in scan_stats)} | "
                f"ai_accepted_normal={sum(item.accepted_normal for item in scan_stats)} | "
                f"pricing_candidates={total_pricing_candidates} | emails_sent={sent} | "
                f"ai_total_seconds={total_ai_seconds:.3f} | "
                f"ai_average_seconds={(total_ai_seconds / total_ai_calls) if total_ai_calls else 0:.3f}"
            )
        if not settings.get("bunjang_sources"):
            print("No Bunjang sources configured in config/settings.json.")
        return 0
    for deal in sample_deals():
        print(format_deal(deal))
    return 0
