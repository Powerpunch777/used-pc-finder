import unittest

from used_pc_finder.parser import (
    exact_model_match,
    is_computer_part,
    is_pricing_identity,
    normalize_product_name,
    parse_price,
)


class ParserTests(unittest.TestCase):
    def test_requested_normalizations(self):
        examples = {
            "4070s 팝니다": "RTX 4070 SUPER",
            "RTX 4070 Super 미개봉": "RTX 4070 SUPER",
            "3060ti 그래픽카드": "RTX 3060 Ti",
            "라이젠 5600x CPU": "Ryzen 5 5600X",
            "Samsung 980 NVMe 500GB": "Samsung 980 500GB",
            "삼성 980 SSD 500기가": "Samsung 980 500GB",
        }
        for title, expected in examples.items():
            with self.subTest(title=title):
                self.assertEqual(normalize_product_name(title), expected)

    def test_unknown_product_returns_none(self):
        self.assertIsNone(normalize_product_name("ordinary desk"))

    def test_numeric_cpu_aliases_do_not_match_memory_or_other_product_families(self):
        contaminated = {
            "DDR5-5600 32GB 램": None,
            "라데온 RX 5600 XT 그래픽카드": None,
            "AMD A8-7600 CPU": None,
            "인텔 i5-7600 CPU": None,
            "RTX 3080 Ti 그래픽카드": None,
        }
        for title, expected in contaminated.items():
            with self.subTest(title=title):
                self.assertEqual(normalize_product_name(title), expected)

    def test_bare_queries_and_intel_aliases_normalize_only_as_complete_tokens(self):
        expected = {
            "5600": "Ryzen 5 5600",
            "7600": "Ryzen 5 7600",
            "14700K": "Intel Core i7-14700K",
            "14600K": "Intel Core i5-14600K",
            "13700K": "Intel Core i7-13700K",
            "13600K": "Intel Core i5-13600K",
            "12700K": "Intel Core i7-12700K",
            "12600K": "Intel Core i5-12600K",
        }
        for title, name in expected.items():
            with self.subTest(title=title):
                self.assertEqual(normalize_product_name(title), name)
        self.assertTrue(exact_model_match("Ryzen 5 5600", "AMD Ryzen 5 5600 CPU"))
        self.assertFalse(exact_model_match("Ryzen 5 5600", "DDR5-5600 32GB"))

    def test_discovery_buckets_cannot_be_pricing_identities(self):
        self.assertFalse(is_pricing_identity("NVMe SSD 2TB"))
        self.assertFalse(is_pricing_identity("B650 Motherboard"))
        self.assertTrue(is_pricing_identity("Samsung 990 PRO 2TB"))

    def test_part_filter(self):
        self.assertTrue(is_computer_part("DDR4 RAM 32GB 판매"))
        self.assertTrue(is_computer_part("컴퓨터 파워 700W"))
        self.assertFalse(is_computer_part("아이패드 케이스"))

    def test_price_parser(self):
        self.assertEqual(parse_price("520,000원"), 520000)
        self.assertIsNone(parse_price("나눔"))


if __name__ == "__main__":
    unittest.main()
