import unittest
from dataclasses import replace

from used_pc_finder.ai_classifier import AIClassification, ClassificationAttempt
from used_pc_finder.bunjang import BunjangPage
from used_pc_finder.cli import backfill_market_price
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


def listing(product_id: str, title: str, *, status: str = "active") -> Listing:
    return Listing(
        title, 150_000, f"https://m.bunjang.co.kr/products/{product_id}", "",
        "bunjang_search", f"bunjang:{product_id}", "정상 작동 CPU입니다.", "normal",
        marketplace="bunjang", product_id=product_id,
        updated_at="2026-08-28T00:00:00Z", listing_status=status,
    )


class FakeCrawler:
    detail_requests = 0

    def __init__(self, values):
        self.values = values

    def search_page(self, _query, _source_key, _cursor=None):
        return BunjangPage(self.values, None, True, search_record_count=len(self.values))

    def inspect(self, value):
        self.detail_requests += 1
        return value


class FakeClassifier:
    model = "test"
    reasoning_effort = "low"

    def classify_attempt(self, value):
        scope, name = {
            "part": ("standalone", "Ryzen 5 5600X"),
            "bundle": ("bundle", "Ryzen 5 5600X"),
            "pc": ("complete_pc", "Ryzen 5 5600X"),
            "wrong": ("standalone", "Ryzen 5 5600"),
        }[value.product_id]
        return ClassificationAttempt(
            AIClassification(True, name, "normal", 0.99, False, scope, scope), 0.01, None
        )


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()
        self.settings = {
            "ai_classification": {
                "enabled": True, "confidence_threshold": 0.85, "ai_concurrency": 2,
            },
            "market_price_estimation": {
                "window_days": 90, "half_life_days": 21,
                "minimum_observations": 1, "estimator": "weighted_median",
            },
        }

    def tearDown(self):
        self.database.close()

    def test_backfill_records_only_active_standalone_exact_model_without_notification(self):
        values = [
            listing("part", "AMD Ryzen 5 5600X"),
            listing("bundle", "Ryzen 5 5600X + B450"),
            listing("pc", "라이젠 5600X 본체"),
            listing("reserved", "AMD Ryzen 5 5600X", status="reserved"),
            listing("wrong", "AMD Ryzen 5 5600"),
        ]
        # The production mode requires a 30-50 result sample. Repeat product IDs
        # here to exercise its search-result cap without creating duplicate records.
        values = (values * 6)[:30]
        result = backfill_market_price(
            self.database, FakeCrawler(values), "Ryzen 5 5600X", self.settings,
            sample_size=30, classifier=FakeClassifier(),
        )

        self.assertEqual(result.search_results_inspected, 30)
        self.assertEqual(result.valid_standalone_listings, 6)
        self.assertEqual(result.new_observations, 1)
        self.assertEqual(result.prices_used, (150_000,))
        self.assertEqual(
            {item.reason for item in result.excluded},
            {"scope:bundle", "scope:complete_pc", "status:reserved", "misclassified_product"},
        )
        observations = self.database.price_observations("Ryzen 5 5600X")
        self.assertEqual([item.product_id for item in observations], ["part"])
        stored = self.database.connection.execute(
            "SELECT notified_at FROM listings WHERE product_id = 'part'"
        ).fetchone()
        self.assertIsNone(stored["notified_at"])

    def test_backfill_is_deliberately_limited_to_the_first_reviewed_product(self):
        with self.assertRaisesRegex(ValueError, "limited to Ryzen 5 5600X"):
            backfill_market_price(
                self.database, FakeCrawler([]), "Ryzen 5 5600", self.settings,
                sample_size=30, classifier=FakeClassifier(),
            )

