import unittest

from used_pc_finder.conditions import classify_condition
from used_pc_finder.config import load_condition_rules
from used_pc_finder.models import Listing
from used_pc_finder.pricing import find_deals


class ConditionTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_condition_rules()
        self.market_prices = {"RTX 4070 SUPER": 600000}

    def test_cheap_broken_rtx_4070_is_rejected_before_price_comparison(self):
        description = "어제까지 잘 쓰다가 작동 안됨. 부품용 고장품입니다."
        status = classify_condition("RTX 4070 SUPER", description, self.rules)
        listing = Listing(
            "RTX 4070 SUPER 팝니다",
            100000,
            "https://example.test/broken-4070",
            "Haan-dong",
            "local",
            description=description,
            condition_status=status,
            ai_scope="standalone",
        )

        self.assertEqual(status, "broken")
        self.assertEqual(find_deals([listing], self.market_prices, 10), [])

    def test_normal_working_rtx_4070_is_allowed_to_be_a_deal(self):
        description = "정상 작동 확인했고 화면 출력과 팬 모두 문제 없습니다."
        status = classify_condition("RTX 4070 SUPER", description, self.rules)
        listing = Listing(
            "RTX 4070 SUPER 팝니다",
            500000,
            "https://example.test/working-4070",
            "Haan-dong",
            "local",
            description=description,
            condition_status=status,
            ai_scope="standalone",
        )

        deals = find_deals([listing], self.market_prices, 10)
        self.assertEqual(status, "normal")
        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0].listing.url, listing.url)

    def test_untested_listing_is_unknown_and_not_a_deal(self):
        status = classify_condition(
            "RTX 4070 SUPER", "작동 미확인 / as-is", self.rules
        )
        self.assertEqual(status, "unknown")

    def test_missing_description_is_unknown(self):
        self.assertEqual(
            classify_condition("RTX 4070 SUPER", "", self.rules), "unknown"
        )

    def test_normal_product_language_is_not_mistaken_for_a_fault(self):
        description = "쿨링팬이 있어 발열 해소에 좋고 정상 작동합니다."
        self.assertEqual(
            classify_condition("GTX 1050", description, self.rules), "normal"
        )


if __name__ == "__main__":
    unittest.main()
