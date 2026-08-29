import unittest
import time
from dataclasses import replace

from used_pc_finder.ai_classifier import AIClassification, ClassificationAttempt
from used_pc_finder.bunjang import BunjangPage, BunjangRequestError
from used_pc_finder.cli import run_full_market_price_backfill
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


def item(product_id: str) -> Listing:
    return Listing(
        "RTX 3070" if product_id == "valid" else "RTX 3070 + B550 세트",
        300_000, f"https://m.bunjang.co.kr/products/{product_id}", "", "bunjang_search",
        f"bunjang:{product_id}", "정상 작동", "normal", marketplace="bunjang",
        product_id=product_id, updated_at="2026-08-28T10:00:00Z",
    )


class FakeCrawler:
    def search_page(self, _query, _source_key, cursor=None):
        if cursor is None:
            return BunjangPage([item("valid")], "second", True)
        return BunjangPage([item("valid"), item("bundle")], None, True)

    def inspect(self, value):
        return value


class DetailFailureCrawler(FakeCrawler):
    def __init__(self):
        self.fail_retry = True
        self.inspected_ids = []

    def search_page(self, _query, _source_key, cursor=None):
        return BunjangPage([item("retry"), item("valid")], None, True)

    def inspect(self, value):
        self.inspected_ids.append(value.product_id)
        if value.product_id == "retry" and self.fail_retry:
            raise BunjangRequestError(
                "detail connection reset",
                exception_type="ConnectionError",
                error_category="connect_timeout",
            )
        return value


class TerminalDetailFailureCrawler(FakeCrawler):
    def search_page(self, _query, _source_key, cursor=None):
        return BunjangPage([item("deleted")], None, True)

    def inspect(self, value):
        raise BunjangRequestError(
            "HTTP 404", http_status=404, exception_type="HTTPError",
            error_category="404_or_unavailable",
        )


class FakeClassifier:
    model = "test"
    reasoning_effort = "low"

    def classify_attempt(self, value):
        scope = "standalone" if value.product_id == "valid" else "bundle"
        return ClassificationAttempt(
            AIClassification(True, "RTX 3070", "normal", 0.99, False, scope, scope),
            0.01,
            None,
        )


class FullBackfillTests(unittest.TestCase):
    settings = {
        "ai_classification": {"enabled": True, "confidence_threshold": 0.85, "ai_concurrency": 4},
        "market_price_estimation": {
            "window_days": 90, "half_life_days": 21,
            "minimum_observations": 1, "estimator": "weighted_median",
        },
    }
    sources = [{"key": "rtx_3070", "query": "RTX 3070"}]

    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()

    def tearDown(self):
        self.database.close()

    def test_resumes_by_cursor_deduplicates_across_pages_and_writes_history(self):
        result = run_full_market_price_backfill(
            self.database, FakeCrawler(), self.sources, self.settings, FakeClassifier(), progress=lambda _line: None
        )

        self.assertEqual((result.listings_inspected, result.valid_observations_collected), (2, 1))
        self.assertEqual((result.excluded_listings, result.completed_queries), (1, 1))
        checkpoint = self.database.backfill_checkpoint("all-market-price-backfill:rtx_3070")
        self.assertEqual((checkpoint["pages_scanned"], checkpoint["completed"]), (2, 1))
        observation = self.database.connection.execute(
            "SELECT observed_at, source_updated_at FROM price_observations"
        ).fetchone()
        self.assertNotEqual(observation["observed_at"], observation["source_updated_at"])
        self.assertEqual(
            self.database.connection.execute("SELECT COUNT(*) FROM market_price_history").fetchone()[0], 1
        )

        resumed = run_full_market_price_backfill(
            self.database, FakeCrawler(), self.sources, self.settings, FakeClassifier(), progress=lambda _line: None
        )
        self.assertEqual((resumed.listings_inspected, resumed.completed_queries), (0, 1))

    def test_detail_failure_is_queued_while_the_page_and_query_continue(self):
        crawler = DetailFailureCrawler()
        first = run_full_market_price_backfill(
            self.database, crawler, self.sources, self.settings, FakeClassifier(), progress=lambda _line: None
        )

        checkpoint = self.database.backfill_checkpoint("all-market-price-backfill:rtx_3070")
        self.assertEqual((first.listings_inspected, first.valid_observations_collected), (2, 1))
        self.assertEqual((checkpoint["pages_scanned"], checkpoint["completed"]), (1, 1))
        self.assertEqual([value.product_id for value in self.database.backfill_detail_retries(checkpoint["query_key"])], ["retry"])
        detail_retry = self.database.connection.execute(
            """SELECT product_id, query_key, http_status, exception_type, error_category,
                      retry_count, last_error_message, last_error_at
               FROM market_price_backfill_detail_retries"""
        ).fetchone()
        self.assertEqual(
            tuple(detail_retry),
            ("retry", checkpoint["query_key"], None, "ConnectionError", "connect_timeout", 0,
             "detail connection reset", detail_retry["last_error_at"]),
        )
        self.assertIsNotNone(detail_retry["last_error_at"])
        self.assertTrue(self.database.is_backfill_product_seen("bunjang", "valid"))
        self.assertFalse(self.database.is_backfill_product_seen("bunjang", "retry"))

        crawler.fail_retry = False
        resumed = run_full_market_price_backfill(
            self.database, crawler, self.sources, self.settings, FakeClassifier(), progress=lambda _line: None
        )

        checkpoint = self.database.backfill_checkpoint("all-market-price-backfill:rtx_3070")
        self.assertEqual((resumed.excluded_listings, resumed.completed_queries), (1, 1))
        self.assertEqual(self.database.backfill_detail_retries(checkpoint["query_key"]), [])
        self.assertTrue(self.database.is_backfill_product_seen("bunjang", "retry"))
        self.assertEqual(checkpoint["excluded_listings"], 1)

    def test_terminal_404_is_audited_marked_unavailable_and_not_requeued(self):
        run_full_market_price_backfill(
            self.database, TerminalDetailFailureCrawler(), self.sources, self.settings,
            FakeClassifier(), progress=lambda _line: None,
        )

        checkpoint = self.database.backfill_checkpoint("all-market-price-backfill:rtx_3070")
        self.assertEqual(self.database.backfill_detail_retries(checkpoint["query_key"]), [])
        diagnostic = self.database.connection.execute(
            """SELECT product_id, http_status, exception_type, error_category, is_terminal
               FROM market_price_backfill_detail_retries"""
        ).fetchone()
        self.assertEqual(
            tuple(diagnostic), ("deleted", 404, "HTTPError", "404_or_unavailable", 1)
        )
        self.assertTrue(self.database.is_backfill_product_seen("bunjang", "deleted"))
        status = self.database.connection.execute(
            "SELECT listing_status FROM listings WHERE product_id = 'deleted'"
        ).fetchone()[0]
        self.assertEqual(status, "unavailable")
        stats = self.database.backfill_detail_retry_statistics(checkpoint["query_key"])
        self.assertEqual(
            [(row["error_category"], row["queued_count"], row["terminal_count"] ) for row in stats],
            [("404_or_unavailable", 0, 1)],
        )

    def test_detail_http_failures_never_enter_ai_review(self):
        class MustNotClassify:
            model = "test"
            reasoning_effort = "low"

            def classify_attempt(self, _value):
                raise AssertionError("unreliable HTTP detail content must not reach AI")

        run_full_market_price_backfill(
            self.database, TerminalDetailFailureCrawler(), self.sources, self.settings,
            MustNotClassify(), progress=lambda _line: None,
        )

    def test_crawling_reaches_later_pages_while_ai_review_is_running(self):
        class SlowClassifier(FakeClassifier):
            def __init__(self):
                self.finished = False

            def classify_attempt(self, value):
                time.sleep(0.05)
                self.finished = True
                return super().classify_attempt(value)

        classifier = SlowClassifier()

        class PipelinedCrawler(FakeCrawler):
            def __init__(self):
                self.second_page_saw_ai_finished = None

            def search_page(self, _query, _source_key, cursor=None):
                if cursor is None:
                    first = replace(item("valid"), title="그래픽카드 판매")
                    return BunjangPage([first], "second", True)
                self.second_page_saw_ai_finished = classifier.finished
                return BunjangPage([], None, True)

        crawler = PipelinedCrawler()
        run_full_market_price_backfill(
            self.database, crawler, self.sources, self.settings, classifier, progress=lambda _line: None
        )
        self.assertFalse(crawler.second_page_saw_ai_finished)
