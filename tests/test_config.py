import json
import tempfile
import unittest
from pathlib import Path

from used_pc_finder.config import load_condition_rules, load_market_prices, load_settings


class ConfigTests(unittest.TestCase):
    def test_load_market_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            path.write_text(json.dumps({"RTX 4070 SUPER": 600000}), encoding="utf-8")
            self.assertEqual(load_market_prices(path)["RTX 4070 SUPER"], 600000)

    def test_default_settings_have_example_locations(self):
        settings = load_settings()
        self.assertEqual(settings["locations"], ["Gwangmyeong", "Haan-dong"])

    def test_default_condition_rules_include_all_risk_statuses(self):
        rules = load_condition_rules()
        self.assertTrue(rules["broken"])
        self.assertTrue(rules["risky"])
        self.assertTrue(rules["unknown"])

    def test_default_bunjang_sources_are_editable_and_use_overlap(self):
        settings = load_settings()
        self.assertEqual(settings["active_marketplace"], "bunjang")
        self.assertEqual(settings["maximum_listing_price"], 500000)
        self.assertEqual(settings["market_price_estimation"]["estimator"], "weighted_median")
        self.assertEqual(len(settings["bunjang_sources"]), 37)
        self.assertIn("RTX 3080", [source["query"] for source in settings["bunjang_sources"]])
        self.assertIn("DDR5 64GB", [source["query"] for source in settings["bunjang_sources"]])
        self.assertTrue(
            all(source["watermark_overlap_pages"] >= 1 for source in settings["bunjang_sources"])
        )


if __name__ == "__main__":
    unittest.main()
