import unittest

from used_pc_finder.pricing import discount_percent


class PricingTests(unittest.TestCase):
    def test_discount(self):
        self.assertAlmostEqual(discount_percent(520000, 600000), 13.333333, places=5)


if __name__ == "__main__":
    unittest.main()
