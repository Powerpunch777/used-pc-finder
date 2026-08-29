import threading
import time
import unittest
from dataclasses import replace

from used_pc_finder.ai_classifier import AIClassification, ClassificationAttempt
from used_pc_finder.cli import AiListingProcessor, AiScanStats, QueuedAiClassification, run_ai_worker_pool
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


def listing(product_id: str = "1", **changes) -> Listing:
    item = Listing(
        "그래픽카드 판매", 320_000,
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
        AIClassification(True, name, "normal", confidence, False, "working complete GPU", "standalone"),
        0.25,
        None,
    )


class AiPipelineTests(unittest.TestCase):
    settings = {"confidence_threshold": 0.85, "ai_concurrency": 5}

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
        self.assertEqual(stats.classified_listings, [result])

    def test_clear_standalone_part_passes_without_ai(self):
        classifier = FakeClassifier(normal_attempt())
        clear = replace(listing(), title="ASUS RTX 3070", description="정상 작동")
        result = self.processor(classifier)(clear)
        self.assertFalse(result.ai_reject)
        self.assertTrue(result.ai_usable_for_market_price)
        self.assertEqual(result.ai_normalized_product_name, "RTX 3070")
        self.assertEqual(classifier.calls, 0)

    def test_ai_must_mark_an_ambiguous_listing_usable_and_active(self):
        rejected = ClassificationAttempt(
            AIClassification(
                True, "RTX 3070", "normal", 0.99, False, "reserved", "standalone",
                "reserved", False,
            ),
            0.01,
            None,
        )
        result = self.processor(FakeClassifier(rejected))(listing())
        self.assertTrue(result.ai_reject)
        self.assertFalse(result.ai_usable_for_market_price)

    def test_low_confidence_result_becomes_unknown(self):
        result = self.processor(FakeClassifier(normal_attempt(0.84)))(listing())
        self.assertTrue(result.ai_reject)
        self.assertEqual(result.condition_status, "unknown")

    def test_ai_bundle_scope_is_rejected_even_with_a_clear_product_name(self):
        attempt = ClassificationAttempt(
            AIClassification(True, "ASUS RTX 3070", "normal", 0.99, False, "GPU and board set", "bundle"),
            0.1,
            None,
        )
        result = self.processor(FakeClassifier(attempt))(listing())
        self.assertTrue(result.ai_reject)
        self.assertEqual(result.ai_scope, "bundle")

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

    def queued_work(self, classifier, count):
        processor = self.processor(classifier)
        queued = [processor.prepare(listing(str(number))) for number in range(count)]
        self.assertTrue(all(isinstance(item, QueuedAiClassification) for item in queued))
        return queued

    def test_worker_pool_never_exceeds_configured_concurrency(self):
        class ConcurrentClassifier(FakeClassifier):
            def __init__(self):
                super().__init__(normal_attempt())
                self.active = self.maximum_active = 0
                self.lock = threading.Lock()

            def classify_attempt(self, item):
                with self.lock:
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                time.sleep(0.02)
                with self.lock:
                    self.active -= 1
                return normal_attempt()

        classifier = ConcurrentClassifier()
        telemetry = run_ai_worker_pool(self.queued_work(classifier, 12), 5)
        self.assertEqual(telemetry.completed_calls, 12)
        self.assertEqual(telemetry.max_concurrency_observed, 5)
        self.assertLessEqual(classifier.maximum_active, 5)

    def test_completed_task_refills_slot_without_waiting_for_other_workers(self):
        class OrderedClassifier(FakeClassifier):
            def __init__(self):
                super().__init__(normal_attempt())
                self.events = []
                self.lock = threading.Lock()

            def classify_attempt(self, item):
                with self.lock:
                    self.events.append(("start", item.product_id, time.monotonic()))
                time.sleep({"0": 0.08, "1": 0.01, "2": 0.01}[item.product_id])
                with self.lock:
                    self.events.append(("finish", item.product_id, time.monotonic()))
                return normal_attempt()

        classifier = OrderedClassifier()
        run_ai_worker_pool(self.queued_work(classifier, 3), 2)
        events = {(kind, product_id): moment for kind, product_id, moment in classifier.events}
        self.assertLess(events[("start", "2")], events[("finish", "0")])

    def test_failed_task_does_not_block_the_queue(self):
        class FailureClassifier(FakeClassifier):
            def __init__(self):
                super().__init__(normal_attempt())
                self.events = []
                self.lock = threading.Lock()

            def classify_attempt(self, item):
                with self.lock:
                    self.events.append(("start", item.product_id, time.monotonic()))
                time.sleep(0.06 if item.product_id == "0" else 0.01)
                with self.lock:
                    self.events.append(("finish", item.product_id, time.monotonic()))
                if item.product_id == "1":
                    return ClassificationAttempt(None, 0.01, "simulated failure")
                return normal_attempt()

        classifier = FailureClassifier()
        telemetry = run_ai_worker_pool(self.queued_work(classifier, 3), 2)
        events = {(kind, product_id): moment for kind, product_id, moment in classifier.events}
        self.assertEqual((telemetry.completed_calls, telemetry.failures), (3, 1))
        self.assertLess(events[("start", "2")], events[("finish", "0")])

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
        row = self.database.connection.execute(
            "SELECT sale_status, usable_for_market_price FROM ai_classifications "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(row), ("active", 1))

    def test_timeout_subprocess_and_unavailable_model_fail_closed(self):
        for error in ("timeout", "subprocess failure", "model unavailable"):
            stats = AiScanStats()
            result = self.processor(
                FakeClassifier(ClassificationAttempt(None, 0.1, error)), stats
            )(listing(error))
            self.assertTrue(result.ai_reject)
            self.assertEqual(result.condition_status, "unknown")
            self.assertEqual(stats.failures, 1)
            self.assertEqual(stats.classified_listings, [result])


if __name__ == "__main__":
    unittest.main()
