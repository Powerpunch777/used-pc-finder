import unittest
from dataclasses import replace

from used_pc_finder.ai_classifier import AIClassification, ClassificationAttempt
from used_pc_finder.cli import AiListingProcessor, AiScanStats
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


def listing(product_id: str = "1", **changes) -> Listing:
    item = Listing(
        "ASUS RTX 3070 그래픽카드", 320_000,
        f"https://example.test/{product_id}", "", "bunjang_search",
        f"bunjang:{product_id}", "정상 작동, 수리 이력 없음", "normal",
        marketplace="bunjang", product_id=product_id,
        updated_at="2026-08-28T00:00:00Z",
    )
    return replace(item, **changes)


class FakeClassifier:
    model = "gpt-5.6-luna"
    reasoning_effort = "low"

    def __init__(self, attempt):
        self.attempt = attempt
        self.calls = 0

    def classify_attempt(self, _listing):
        self.calls += 1
        return self.attempt


def normal_attempt(confidence: float = 0.95, name: str = "ASUS RTX 3070"):
    return ClassificationAttempt(
        AIClassification(True, name, "normal", confidence, False, "working complete GPU"),
        0.25,
        None,
    )


class AiPipelineTests(unittest.TestCase):
    settings = {"confidence_threshold": 0.85, "max_ai_calls_per_scan": 10}

    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()

    def tearDown(self):
        self.database.close()

    def processor(self, classifier, stats=None, settings=None):
        return AiListingProcessor(
            self.database, classifier, settings or self.settings, stats or AiScanStats()
        )

    def test_high_confidence_normal_result_is_canonically_accepted(self):
        stats = AiScanStats()
        result = self.processor(FakeClassifier(normal_attempt()), stats)(listing())
        self.assertFalse(result.ai_reject)
        self.assertEqual(result.condition_status, "normal")
        self.assertEqual(result.ai_normalized_product_name, "RTX 3070")
        self.assertEqual(stats.accepted_normal, 1)

    def test_low_confidence_result_becomes_unknown(self):
        result = self.processor(FakeClassifier(normal_attempt(0.84)))(listing())
        self.assertTrue(result.ai_reject)
        self.assertEqual(result.condition_status, "unknown")

    def test_broken_and_non_part_listings_are_rejected_without_ai(self):
        classifier = FakeClassifier(normal_attempt())
        broken = self.processor(classifier)(listing(condition_status="broken"))
        unrelated = self.processor(classifier)(listing("2", title="아이패드", description="정상 작동"))
        self.assertTrue(broken.ai_reject)
        self.assertTrue(unrelated.ai_reject)
        self.assertEqual(classifier.calls, 0)

    def test_canonical_normalization_failure_becomes_unknown(self):
        result = self.processor(FakeClassifier(normal_attempt(name="Mystery Card X")))(listing())
        self.assertTrue(result.ai_reject)
        self.assertEqual(result.condition_status, "unknown")
        self.assertIsNone(result.ai_normalized_product_name)

    def test_max_call_count_leaves_remaining_listing_unclassified(self):
        stats = AiScanStats()
        classifier = FakeClassifier(normal_attempt())
        processor = self.processor(classifier, stats, {**self.settings, "max_ai_calls_per_scan": 1})
        first = processor(listing("1"))
        second = processor(listing("2"))
        self.assertFalse(first.ai_reject)
        self.assertIsNone(second.ai_is_computer_part)
        self.assertEqual(stats.calls, 1)
        self.assertEqual(stats.deferred, 1)

    def test_cached_result_is_reused_but_content_change_reclassifies(self):
        stats = AiScanStats()
        classifier = FakeClassifier(normal_attempt())
        processor = self.processor(classifier, stats)
        original = listing()
        processor(original)
        processor(original)
        processor(replace(original, description="정상 작동, 써멀 재도포 완료"))
        self.assertEqual(classifier.calls, 2)
        self.assertEqual(stats.cached, 1)

    def test_timeout_subprocess_and_unavailable_model_fail_closed(self):
        for error in ("timeout", "subprocess failure", "model unavailable"):
            stats = AiScanStats()
            result = self.processor(
                FakeClassifier(ClassificationAttempt(None, 0.1, error)), stats
            )(listing(error))
            self.assertTrue(result.ai_reject)
            self.assertEqual(result.condition_status, "unknown")
            self.assertEqual(stats.failures, 1)


if __name__ == "__main__":
    unittest.main()
