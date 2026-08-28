import unittest

from used_pc_finder.parser import is_computer_part, normalize_product_name, parse_price


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

    def test_part_filter(self):
        self.assertTrue(is_computer_part("DDR4 RAM 32GB 판매"))
        self.assertTrue(is_computer_part("컴퓨터 파워 700W"))
        self.assertFalse(is_computer_part("아이패드 케이스"))

    def test_price_parser(self):
        self.assertEqual(parse_price("520,000원"), 520000)
        self.assertIsNone(parse_price("나눔"))


if __name__ == "__main__":
    unittest.main()
