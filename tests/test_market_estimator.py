from datetime import UTC, datetime, timedelta
import unittest

from used_pc_finder.database import ListingDatabase
from used_pc_finder.market_estimator import (
    PriceObservation,
    estimate_market_price,
    estimation_observations,
)
from used_pc_finder.models import Listing


NOW = datetime(2026, 8, 28, tzinfo=UTC)


def observation(price: int, age_days: int, product_id: str = "1") -> PriceObservation:
    timestamp = (NOW - timedelta(days=age_days)).isoformat().replace("+00:00", "Z")
    return PriceObservation(
        "bunjang", product_id, "RTX 3070", price, timestamp, timestamp, timestamp
    )


class MarketEstimatorTests(unittest.TestCase):
    def test_recent_observation_has_more_influence_than_old_observation(self):
        estimate = estimate_market_price(
            "RTX 3070", [observation(600_000, 60), observation(300_000, 0)],
            manual_reference_price=400_000,
            minimum_observations=2,
            estimator="weighted_mean",
            now=NOW,
        )
        self.assertTrue(estimate.automatic)
        self.assertLess(estimate.price, 350_000)

    def test_sixty_day_old_high_price_has_little_effect_on_weighted_median(self):
        estimate = estimate_market_price(
            "RTX 3070", [observation(600_000, 60), observation(300_000, 0)],
            manual_reference_price=400_000,
            minimum_observations=2,
            now=NOW,
        )
        self.assertTrue(estimate.automatic)
        self.assertEqual(estimate.estimator, "weighted_median")
        self.assertEqual(estimate.price, 300_000)

    def test_price_reduction_on_old_listing_creates_fresh_observation(self):
        database = ListingDatabase(":memory:")
        database.initialize()
        try:
            old = Listing(
                "RTX 3070", 500_000, "https://example/1", "", "bunjang_search",
                "bunjang:1", "정상 작동", "normal", marketplace="bunjang",
                product_id="1", updated_at="2026-06-01T00:00:00Z",
                ai_scope="standalone", ai_usable_for_market_price=True,
                ai_usable_price=True, effective_price=500_000,
            )
            self.assertTrue(database.add(old))
            database.record_price_observation(
                old, "RTX 3070", observed_at="2026-06-01T00:00:00Z"
            )
            reduced = Listing(
                "RTX 3070", 350_000, "https://example/1", "", "bunjang_search",
                "bunjang:1", "정상 작동", "normal", marketplace="bunjang",
                product_id="1", updated_at="2026-08-28T00:00:00Z",
                ai_scope="standalone", ai_usable_for_market_price=True,
                ai_usable_price=True, effective_price=350_000,
            )
            state = database.candidate_state(reduced)
            database.store_processed(reduced, state)
            database.record_price_observation(
                reduced, "RTX 3070", observed_at="2026-08-28T00:00:00Z"
            )
            observations = database.price_observations("RTX 3070")
        finally:
            database.close()

        self.assertEqual([item.observed_price for item in observations], [500_000, 350_000])
        self.assertEqual(observations[0].first_seen_at, observations[1].first_seen_at)
        estimate = estimate_market_price(
            "RTX 3070", observations, manual_reference_price=400_000,
            minimum_observations=2, now=NOW,
        )
        self.assertEqual(estimate.price, 350_000)

    def test_mad_outlier_removal_keeps_unrealistic_price_from_distorting_estimate(self):
        estimate = estimate_market_price(
            "RTX 3070",
            [
                observation(300_000, 1, "1"), observation(305_000, 1, "2"),
                observation(310_000, 1, "3"), observation(295_000, 1, "4"),
                observation(302_000, 1, "5"), observation(2_000_000, 1, "6"),
            ],
            manual_reference_price=400_000,
            now=NOW,
        )
        self.assertTrue(estimate.automatic)
        self.assertEqual(estimate.valid_observation_count, 5)
        self.assertEqual(estimate.price, 302_000)

    def test_fewer_than_five_valid_observations_uses_manual_fallback(self):
        estimate = estimate_market_price(
            "RTX 3070", [observation(300_000, age) for age in range(4)],
            manual_reference_price=420_000,
            now=NOW,
        )
        self.assertFalse(estimate.automatic)
        self.assertEqual(estimate.estimator, "manual_fallback")
        self.assertEqual(estimate.price, 420_000)

    def test_zero_mad_does_not_drop_non_modal_genuine_observations(self):
        values = [observation(170_000, 1, str(number)) for number in range(5)]
        values.append(observation(150_000, 1, "other"))
        used = estimation_observations(values, now=NOW, window_days=90)
        self.assertEqual(len(used), 6)


if __name__ == "__main__":
    unittest.main()
