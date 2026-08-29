import unittest
from dataclasses import replace

from used_pc_finder.ai_classifier import AIClassification
from used_pc_finder.cli import format_deal, sample_deals
from used_pc_finder.models import Listing
from used_pc_finder.pricing import find_deals
from used_pc_finder.cli import classify_new_listing


class PipelineTests(unittest.TestCase):
    def test_requested_sample_is_13_point_3_percent_cheaper(self):
        deals = sample_deals()
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].normalized_name, "RTX 4070 SUPER")
        self.assertEqual(deals[0].listing.price, 520000)
        self.assertEqual(deals[0].reference_price, 600000)
        self.assertAlmostEqual(deals[0].discount_percent, 13.3, places=1)
        output = format_deal(deals[0])
        self.assertIn("13.3% cheaper", output)
        self.assertIn(deals[0].listing.url, output)

    def test_deals_are_sorted_highest_discount_first(self):
        listings = [
            Listing(
                "3060ti", 270000, "https://example/1", "Haan", "local",
                condition_status="normal",
                ai_scope="standalone",
            ),
            Listing(
                "4070s", 480000, "https://example/2", "Haan", "buy_now",
                condition_status="normal",
                ai_scope="standalone",
            ),
        ]
        deals = find_deals(
            listings, {"RTX 3060 Ti": 300000, "RTX 4070 SUPER": 600000}, 10
        )
        self.assertEqual([deal.normalized_name for deal in deals], [
            "RTX 4070 SUPER", "RTX 3060 Ti"
        ])

    def test_non_deal_and_unknown_product_are_excluded(self):
        listings = [
            Listing(
                "4070s", 590000, "https://example/1", "Haan", "local",
                condition_status="normal",
            ),
            Listing(
                "DDR4 RAM", 10000, "https://example/2", "Haan", "local",
                condition_status="normal",
            ),
        ]
        self.assertEqual(find_deals(listings, {"RTX 4070 SUPER": 600000}, 10), [])

    def test_cheap_broken_rule_skips_the_ai_classifier(self):
        class MustNotBeCalled:
            def classify(self, _listing):
                raise AssertionError("AI must not receive broken listings")

        item = Listing(
            "RTX 4070 SUPER 고장",
            100000,
            "https://example/broken",
            "Haan",
            "local",
            description="작동 안됨, 부품용입니다.",
            condition_status="broken",
        )
        classified = classify_new_listing(
            item, MustNotBeCalled(), {"minimum_confidence": 0.9}
        )
        self.assertTrue(classified.ai_reject)
        self.assertEqual(classified.ai_confidence, 0.0)

    def test_high_confidence_normal_ai_result_reaches_price_comparison(self):
        class Classifier:
            def classify(self, _listing):
                return AIClassification(
                    True, "RTX 4070 SUPER", "normal", 0.98, False, "Working GPU", "standalone"
                )

        item = Listing(
            "판매합니다",
            500000,
            "https://example/working",
            "Haan",
            "local",
            description="RTX 4070 SUPER 정상 작동 제품입니다.",
            condition_status="normal",
        )
        classified = classify_new_listing(
            item, Classifier(), {"minimum_confidence": 0.9}
        )
        self.assertFalse(classified.ai_reject)
        self.assertEqual(
            len(find_deals([classified], {"RTX 4070 SUPER": 600000}, 10)), 1
        )

    def test_ai_failure_is_rejected_and_cannot_be_a_deal(self):
        class FailedClassifier:
            def classify(self, _listing):
                return None

        item = Listing(
            "그래픽카드 판매",
            500000,
            "https://example/failed-ai",
            "Haan",
            "local",
            description="정상 작동 제품입니다.",
            condition_status="normal",
        )
        classified = classify_new_listing(
            item, FailedClassifier(), {"minimum_confidence": 0.9}
        )
        self.assertTrue(classified.ai_reject)
        self.assertEqual(find_deals([classified], {"RTX 4070 SUPER": 600000}, 10), [])

    def test_only_active_standalone_parts_are_price_comparable(self):
        standalone = Listing(
            "RTX 4070 SUPER", 500000, "https://example/part", "Haan", "local",
            description="정상 작동", condition_status="normal", ai_is_computer_part=True,
            ai_normalized_product_name="RTX 4070 SUPER", ai_confidence=0.99,
            ai_scope="standalone", listing_status="active",
        )
        prices = {"RTX 4070 SUPER": 600000}
        self.assertEqual(len(find_deals([standalone], prices, 10, require_ai=True)), 1)
        for blocked in (
            replace(standalone, listing_status="sold"),
            replace(standalone, listing_status="reserved"),
            replace(standalone, listing_status="unavailable"),
            replace(standalone, ai_scope="complete_pc"),
            replace(standalone, ai_scope="bundle"),
            replace(standalone, ai_scope="accessory"),
            replace(standalone, ai_scope="unknown"),
        ):
            self.assertEqual(find_deals([blocked], prices, 10, require_ai=True), [])


if __name__ == "__main__":
    unittest.main()
