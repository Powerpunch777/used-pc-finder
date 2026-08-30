import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from used_pc_finder.database import ListingDatabase
from used_pc_finder.final_review import (
    CodexTextFinalReviewer,
    FinalEmailReviewGate,
    FinalReviewAttempt,
    FinalReviewResult,
)
from used_pc_finder.models import Deal, Listing


SETTINGS = {
    "enabled": True, "command": "codex", "model": "gpt-5.6-terra",
    "reasoning_effort": "high", "timeout_seconds": 10,
    "confidence_threshold": 0.9, "schema_path": "config/final_email_review_schema.json",
}


def part_listing(**changes):
    values = dict(
        title="MSI RTX 4070 SUPER 그래픽카드", price=500000,
        url="https://m.bunjang.co.kr/products/test-4070", location="Seoul",
        source_type="bunjang_search", listing_id="bunjang:test-4070",
        description="RTX 4070 SUPER 그래픽카드 정상 작동합니다.",
        condition_status="normal", ai_is_computer_part=True,
        ai_normalized_product_name="RTX 4070 SUPER", ai_confidence=0.99,
        marketplace="bunjang", product_id="test-4070",
        canonical_url="https://m.bunjang.co.kr/products/test-4070", listing_status="active",
        ai_scope="standalone", ai_sale_status="active", ai_usable_for_market_price=True,
        ai_usable_price=True, effective_price=500000,
    )
    values.update(changes)
    return Listing(**values)


def bargain(**listing_changes):
    listing = part_listing(**listing_changes)
    return Deal(listing, "RTX 4070 SUPER", 600000, 16.6667, 500000)


def approved(**changes):
    values = dict(
        exact_product=True, normalized_product_name="RTX 4070 SUPER", scope="standalone",
        condition="normal", sale_status="active", model_mismatch=False,
        displayed_price=500000, effective_price=500000, price_bait=False,
        hidden_price_condition=False, usable_price=True, price_confidence=0.96,
        confidence=0.96,
        reason="exact active standalone GPU",
    )
    values.update(changes)
    return FinalReviewResult(**values)


class FakeCrawler:
    def __init__(self, product=None, error=None):
        self.product = product or {
            "name": "MSI RTX 4070 SUPER 그래픽카드",
            "description": "RTX 4070 SUPER 그래픽카드 정상 작동합니다.",
            "price": 500000, "saleStatus": "SELLING",
        }
        self.error = error
        self.calls = 0

    def fetch_product_detail(self, _product_id):
        self.calls += 1
        if self.error:
            raise self.error
        return self.product


class FakeReviewer:
    model = "gpt-5.6-terra"
    reasoning_effort = "high"

    def __init__(self, result=None, error=None):
        self.result = approved() if result is None and error is None else result
        self.error = error
        self.calls = 0

    def review_attempt(self, *_args, **_kwargs):
        self.calls += 1
        return FinalReviewAttempt(self.result if self.error is None else None, 0.01, self.error)


class FinalReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()
        self.addCleanup(self.database.close)

    def gate(self, crawler=None, reviewer=None):
        return FinalEmailReviewGate(
            self.database, crawler or FakeCrawler(), reviewer or FakeReviewer(), SETTINGS,
        )

    def test_approved_candidate_is_refetched_text_reviewed_and_persisted(self):
        deal = bargain()
        self.database.add(deal.listing)
        decision = self.gate().review_deal(deal, minimum_discount_percent=10)
        self.assertTrue(decision.passed)
        row = self.database.connection.execute(
            "select review_status, reviewed_price, image_count, success from final_email_reviews"
        ).fetchone()
        self.assertEqual(tuple(row), ("approved", 500000, 0, 1))

    def test_sold_changed_price_and_model_mismatch_fail_before_ai(self):
        cases = [{"saleStatus": "SOLD"}, {"price": 550000}, {
            "name": "RTX 4060 그래픽카드", "description": "RTX 4060 입니다",
        }]
        for index, product_changes in enumerate(cases):
            with self.subTest(product_changes=product_changes):
                deal = bargain(product_id=f"test-precheck-{index}")
                self.database.add(deal.listing)
                product = dict(FakeCrawler().product)
                product.update(product_changes)
                reviewer = FakeReviewer()
                decision = self.gate(FakeCrawler(product), reviewer).review_deal(deal, minimum_discount_percent=10)
                self.assertFalse(decision.passed)
                self.assertEqual(reviewer.calls, 0)

    def test_bundle_and_first_stage_price_rejection_never_reach_final_ai(self):
        for changes in ({"ai_scope": "bundle"}, {"ai_usable_price": False}):
            with self.subTest(changes=changes):
                deal = bargain(**changes)
                self.database.add(deal.listing)
                reviewer = FakeReviewer()
                self.assertFalse(self.gate(reviewer=reviewer).review_deal(deal, minimum_discount_percent=10).passed)
                self.assertEqual(reviewer.calls, 0)

    def test_model_mismatch_low_confidence_and_timeout_fail_closed(self):
        variants = [approved(model_mismatch=True), approved(confidence=0.89), None]
        for index, result in enumerate(variants):
            with self.subTest(result=result):
                deal = bargain(product_id=f"test-fail-{index}")
                self.database.add(deal.listing)
                reviewer = FakeReviewer(result=result, error="timeout" if result is None else None)
                decision = self.gate(reviewer=reviewer).review_deal(deal, minimum_discount_percent=10)
                self.assertFalse(decision.passed)
                self.assertFalse(self.database.was_notified(deal.listing))

    def test_factual_valid_bargain_is_not_rejected_by_a_historical_ai_send_flag(self):
        """The application decides delivery from factual checks, never AI send_email."""
        deal = bargain()
        self.database.add(deal.listing)
        result = SimpleNamespace(
            exact_product=True, normalized_product_name="RTX 4070 SUPER", scope="standalone",
            condition="normal", sale_status="active", model_mismatch=False,
            displayed_price=500000, effective_price=500000, price_bait=False,
            hidden_price_condition=False, usable_price=True, price_confidence=0.96,
            confidence=0.96, reason="factual approval", send_email=False,
        )
        decision = self.gate(reviewer=FakeReviewer(result=result)).review_deal(
            deal, minimum_discount_percent=10
        )
        self.assertTrue(decision.passed)

    def test_cached_unchanged_text_review_is_reused_without_second_ai_call(self):
        deal = bargain()
        self.database.add(deal.listing)
        reviewer = FakeReviewer()
        gate = self.gate(reviewer=reviewer)
        self.assertTrue(gate.review_deal(deal, minimum_discount_percent=10).passed)
        second = gate.review_deal(deal, minimum_discount_percent=10)
        self.assertTrue(second.passed)
        self.assertTrue(second.cached)
        self.assertEqual(reviewer.calls, 1)


class FinalReviewerCliFailureTests(unittest.TestCase):
    def schema(self, directory):
        path = Path(directory) / "schema.json"
        path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
        return path

    def test_timeout_and_invalid_json_return_no_result(self):
        with tempfile.TemporaryDirectory() as directory:
            schema = self.schema(directory)
            listing, deal = part_listing(), bargain()

            def timeout(*_args, **_kwargs):
                raise subprocess.TimeoutExpired("codex", 1)

            self.assertIsNone(CodexTextFinalReviewer(schema, runner=timeout).review_attempt(listing, deal).result)

            def invalid(args, **_kwargs):
                Path(args[args.index("--output-last-message") + 1]).write_text("{not json", encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "")

            self.assertIsNone(CodexTextFinalReviewer(schema, runner=invalid).review_attempt(listing, deal).result)


if __name__ == "__main__":
    unittest.main()
