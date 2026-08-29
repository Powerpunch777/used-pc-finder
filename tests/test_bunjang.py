import unittest
from dataclasses import replace

import requests

from used_pc_finder.ai_classifier import AIClassification, ClassificationAttempt
from used_pc_finder.bunjang import BunjangCrawler, BunjangPage, BunjangRequestError, SEARCH_URL
from used_pc_finder.bunjang_scan import scan_bunjang_source
from used_pc_finder.cli import AiListingProcessor, AiScanStats
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


def candidate(product_id: str, updated_at: str, price: int = 500_000) -> Listing:
    return Listing(
        title="RTX 4070 SUPER",
        price=price,
        url=f"https://m.bunjang.co.kr/products/{product_id}",
        location="",
        source_type="bunjang_search",
        listing_id=f"bunjang:{product_id}",
        marketplace="bunjang",
        product_id=product_id,
        source_key="gpu",
        updated_at=updated_at,
        search_fingerprint="same-search-content",
        ai_is_computer_part=True,
        ai_normalized_product_name="RTX 4070 SUPER",
        ai_scope="standalone",
    )


class FakeCrawler:
    def __init__(self, pages):
        self.pages = pages
        self.detail_requests = 0
        self.queries = []

    def search_page(self, query, source_key, cursor=None):
        self.queries.append((query, source_key, cursor))
        return self.pages[cursor or "first"]

    def inspect(self, listing):
        self.detail_requests += 1
        return replace(
            listing,
            description="정상 작동 제품입니다.",
            condition_status="normal",
        )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.payload)


class SequencedSession:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append((url, params, timeout))
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ErrorResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        error = requests.HTTPError(f"HTTP {self.status_code}")
        error.response = self
        raise error


class BunjangCrawlerTests(unittest.TestCase):
    @staticmethod
    def empty_search_payload():
        return {"data": {"responses": {"mainGrid": {"searchResponse": {"data": []}}}}}

    def test_search_captures_public_metadata_and_excludes_ads(self):
        payload = {
            "data": {"responses": {"mainGrid": {"searchResponse": {
                "cursor": "next-page",
                "data": [
                    {
                        "type": "PRODUCT", "status": "SELLING", "pid": 101,
                        "name": "RTX 4070 SUPER", "price": 500000,
                        "updatedAt": "2026-08-28T10:00:00+09:00", "category": "GPU",
                    },
                    {
                        "type": "PRODUCT", "status": "SELLING", "pid": 105,
                        "name": "over budget", "price": 500001,
                        "updatedAt": "2026-08-28T09:59:30+09:00", "category": "GPU",
                    },
                    {
                        "type": "EXT_AD", "status": "SELLING", "pid": 102,
                        "name": "sponsored", "price": 1,
                        "updatedAt": "2026-08-28T09:59:00+09:00",
                    },
                    {
                        "type": "PRODUCT", "status": "SELLING", "ad": True, "pid": 103,
                        "name": "ad card", "price": 1,
                        "updatedAt": "2026-08-28T09:58:00+09:00",
                    },
                    {
                        "type": "PRODUCT", "status": "SOLD", "pid": 104,
                        "name": "sold", "price": 1,
                        "updatedAt": "2026-08-28T09:57:00+09:00",
                    },
                ],
            }}}}
        }
        crawler = BunjangCrawler(
            delay_seconds=0, maximum_listing_price=500000, sleep=lambda _seconds: None
        )
        session = FakeSession(payload)
        crawler.session = session

        page = crawler.search_page("RTX 4070", "gpu")

        self.assertEqual(len(page.listings), 2)
        listing = page.listings[0]
        self.assertEqual(listing.product_id, "101")
        self.assertEqual(listing.title, "RTX 4070 SUPER")
        self.assertEqual(listing.price, 500000)
        self.assertEqual(listing.updated_at, "2026-08-28T10:00:00+09:00")
        self.assertEqual(listing.url, "https://m.bunjang.co.kr/products/101")
        self.assertEqual(listing.listing_status, "active")
        self.assertEqual(page.listings[1].listing_status, "sold")
        self.assertEqual(session.calls[0][0], SEARCH_URL)
        self.assertEqual(session.calls[0][1]["sort"], "latest")
        self.assertTrue(page.is_monotonic_descending)
        self.assertEqual(page.over_budget_count, 1)
        self.assertEqual(page.irrelevant_count, 2)
        self.assertEqual(page.search_record_count, 5)

    def test_detail_keeps_the_search_timestamp_for_incremental_comparison(self):
        payload = {
            "data": {"product": {
                "name": "RTX 4070 SUPER", "price": 500000,
                "description": "정상 작동 제품입니다.",
                "updatedAt": "2026-08-28T10:00:00.123456Z",
            }}
        }
        crawler = BunjangCrawler(delay_seconds=0, sleep=lambda _seconds: None)
        crawler.session = FakeSession(payload)
        inspected = crawler.inspect(candidate("1", "2026-08-28T10:00:00Z"))
        self.assertEqual(inspected.updated_at, "2026-08-28T10:00:00Z")

    def test_transient_timeout_and_5xx_retry_with_exponential_backoff(self):
        sleeps = []
        crawler = BunjangCrawler(
            delay_seconds=0,
            sleep=sleeps.append,
            max_retries=2,
            retry_backoff_seconds=0.5,
        )
        session = SequencedSession([
            requests.ReadTimeout("read timed out"),
            ErrorResponse(503),
            FakeResponse(self.empty_search_payload()),
        ])
        crawler.session = session

        page = crawler.search_page("RTX 4070", "gpu")

        self.assertEqual(page.listings, [])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertEqual(crawler.request_retries, 2)
        self.assertEqual(crawler.request_failures, 0)

    def test_permanent_4xx_is_not_retried(self):
        crawler = BunjangCrawler(delay_seconds=0, sleep=lambda _seconds: None)
        session = SequencedSession([ErrorResponse(404)])
        crawler.session = session

        with self.assertRaises(BunjangRequestError) as raised:
            crawler.search_page("RTX 4070", "gpu")

        self.assertEqual(len(session.calls), 1)
        self.assertEqual(crawler.request_retries, 0)
        self.assertEqual(crawler.permanent_failures, 1)
        self.assertEqual(crawler.request_failures, 1)
        self.assertEqual(raised.exception.http_status, 404)
        self.assertEqual(raised.exception.exception_type, "HTTPError")
        self.assertEqual(raised.exception.error_category, "404_or_unavailable")
        self.assertEqual(raised.exception.retry_count, 0)

    def test_429_retries_with_bounded_exponential_backoff(self):
        sleeps = []
        crawler = BunjangCrawler(
            delay_seconds=0, sleep=sleeps.append, max_retries=2, retry_backoff_seconds=0.5
        )
        session = SequencedSession([ErrorResponse(429), FakeResponse(self.empty_search_payload())])
        crawler.session = session

        crawler.search_page("RTX 4070", "gpu")

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [0.5])


class BunjangScanTests(unittest.TestCase):
    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()
        self.source = {
            "key": "gpu", "query": "그래픽카드", "max_pages": 4,
            "watermark_overlap_pages": 1,
        }

    def tearDown(self):
        self.database.close()

    @staticmethod
    def process(listing):
        return listing

    def scan(self, crawler, seen=None, process=None):
        return scan_bunjang_source(
            crawler, self.database, self.source, process or self.process,
            seen if seen is not None else set(),
        )

    def test_new_item_is_inspected_and_saved(self):
        crawler = FakeCrawler({
            "first": BunjangPage([candidate("1", "2026-08-28T10:00:00+09:00")], None, True)
        })
        result = self.scan(crawler)
        self.assertEqual((result.new_count, result.updated_count, result.unchanged_count), (1, 0, 0))
        self.assertEqual(result.pending_ai_count, 0)
        self.assertEqual(result.detail_requests, 1)
        self.assertEqual(self.database.count(), 1)

    def test_failed_search_is_logged_and_skipped_without_stopping_the_scan(self):
        class FailingSearchCrawler(FakeCrawler):
            def search_page(self, query, source_key, cursor=None):
                raise BunjangRequestError("temporary Bunjang outage")

        result = self.scan(FailingSearchCrawler({}))
        self.assertEqual(result.pages_fetched, 0)
        self.assertEqual(result.search_records_fetched, 0)
        self.assertEqual(result.listings, [])

    def test_page_counter_does_not_inherit_prior_detail_requests(self):
        crawler = FakeCrawler({
            "first": BunjangPage([candidate("1", "2026-08-28T10:00:00+09:00")], None, True)
        })
        crawler.detail_requests = 9
        result = self.scan(crawler)
        self.assertEqual(result.pages_fetched, 1)
        self.assertEqual(result.detail_requests, 1)

    def test_unchanged_existing_item_skips_detail_fetching(self):
        item = candidate("1", "2026-08-28T10:00:00+09:00")
        self.database.add(item)
        crawler = FakeCrawler({"first": BunjangPage([item], None, True)})
        result = self.scan(crawler)
        self.assertEqual(result.unchanged_count, 1)
        self.assertEqual(result.detail_requests, 0)

    def test_detail_timestamp_precision_does_not_make_an_item_changed(self):
        stored = candidate("1", "2026-08-28T10:00:00.123456Z")
        self.database.add(stored)
        public_search = candidate("1", "2026-08-28T10:00:00Z")
        crawler = FakeCrawler({"first": BunjangPage([public_search], None, True)})
        result = self.scan(crawler)
        self.assertEqual(result.unchanged_count, 1)
        self.assertEqual(result.detail_requests, 0)

    def test_price_changed_existing_item_is_reprocessed(self):
        old = candidate("1", "2026-08-28T10:00:00+09:00", 550_000)
        self.database.add(old)
        self.database.mark_notified(old)
        changed = candidate("1", "2026-08-28T10:00:00+09:00", 500_000)
        crawler = FakeCrawler({"first": BunjangPage([changed], None, True)})
        result = self.scan(crawler)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.detail_requests, 1)
        self.assertFalse(self.database.was_notified(changed))
        observations = self.database.price_observations("RTX 4070 SUPER")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].observed_price, 500_000)

    def test_price_increase_does_not_reopen_a_notified_listing(self):
        old = candidate("1", "2026-08-28T10:00:00+09:00", 500_000)
        self.database.add(old)
        self.database.mark_notified(old)
        increased = candidate("1", "2026-08-28T10:00:00+09:00", 550_000)
        crawler = FakeCrawler({"first": BunjangPage([increased], None, True)})

        result = self.scan(crawler)

        self.assertEqual(result.updated_count, 1)
        self.assertTrue(self.database.was_notified(increased))

    def test_updated_at_changed_existing_item_is_reprocessed(self):
        old = candidate("1", "2026-08-28T10:00:00+09:00")
        self.database.add(old)
        changed = candidate("1", "2026-08-28T11:00:00+09:00")
        crawler = FakeCrawler({"first": BunjangPage([changed], None, True)})
        result = self.scan(crawler)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.detail_requests, 1)

    def test_duplicate_product_across_queries_is_not_processed_twice(self):
        item = candidate("1", "2026-08-28T10:00:00+09:00")
        first = FakeCrawler({"first": BunjangPage([item], None, True)})
        seen = set()
        self.scan(first, seen)
        second = FakeCrawler({"first": BunjangPage([item], None, True)})
        result = self.scan(second, seen)
        self.assertEqual(first.detail_requests, 1)
        self.assertEqual(second.detail_requests, 0)
        self.assertEqual(result.duplicate_count, 1)

    def test_cross_query_duplicate_creates_only_one_ai_call(self):
        class CountingClassifier:
            model = "gpt-5.6-luna"
            reasoning_effort = "low"

            def __init__(self):
                self.calls = 0

            def classify_attempt(self, _listing):
                self.calls += 1
                return ClassificationAttempt(
                    AIClassification(True, "RTX 4070 SUPER", "normal", 0.99, False, "working", "standalone"),
                    0.01,
                    None,
                )

        classifier = CountingClassifier()
        processor = AiListingProcessor(
            self.database,
            classifier,
            {"confidence_threshold": 0.85, "ai_concurrency": 5},
            AiScanStats(),
        )
        item = candidate("1", "2026-08-28T10:00:00+09:00")
        seen = set()
        self.scan(FakeCrawler({"first": BunjangPage([item], None, True)}), seen, processor)
        self.scan(FakeCrawler({"first": BunjangPage([item], None, True)}), seen, processor)
        self.assertEqual(classifier.calls, 1)

    def test_unchanged_listing_creates_zero_ai_calls(self):
        class MustNotClassify:
            model = "gpt-5.6-luna"
            reasoning_effort = "low"
            calls = 0

            def classify_attempt(self, _listing):
                self.calls += 1
                raise AssertionError("unchanged listings must not reach AI")

        item = candidate("1", "2026-08-28T10:00:00+09:00")
        self.database.add(item)
        classifier = MustNotClassify()
        processor = AiListingProcessor(
            self.database,
            classifier,
            {"confidence_threshold": 0.85, "ai_concurrency": 5},
            AiScanStats(),
        )
        self.scan(FakeCrawler({"first": BunjangPage([item], None, True)}), process=processor)
        self.assertEqual(classifier.calls, 0)

    def test_watermark_uses_an_extra_old_page_before_stopping(self):
        first = candidate("1", "2026-08-26T10:00:00+09:00")
        second = candidate("2", "2026-08-25T10:00:00+09:00")
        self.database.add(first)
        self.database.add(second)
        self.database.set_watermark("bunjang", "gpu", "2026-08-27T10:00:00+09:00")
        crawler = FakeCrawler({
            "first": BunjangPage([first], "page-2", True),
            "page-2": BunjangPage([second], "page-3", True),
            "page-3": BunjangPage([], None, True),
        })
        result = self.scan(crawler)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(result.unchanged_count, 2)
        self.assertTrue(result.ordering_monotonic)
        self.assertTrue(result.stopped_at_watermark)


if __name__ == "__main__":
    unittest.main()
