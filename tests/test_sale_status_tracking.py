import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from used_pc_finder.cli import track_sale_statuses
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


TRACKING = {
    "recent_age_days": 3,
    "medium_age_days": 14,
    "recent_interval_hours": 6,
    "medium_interval_hours": 24,
    "older_interval_hours": 168,
}


def active_listing(product_id: str, price: int = 150_000) -> Listing:
    return Listing(
        "AMD Ryzen 5 5600X", price, f"https://m.bunjang.co.kr/products/{product_id}", "",
        "bunjang_search", f"bunjang:{product_id}", "정상 작동 CPU", "normal",
        True, "Ryzen 5 5600X", 0.99, False, "working CPU",
        marketplace="bunjang", product_id=product_id, listing_status="active",
        ai_scope="standalone",
    )


class FakeCrawler:
    def __init__(self, statuses):
        self.statuses = statuses
        self.calls = []

    def inspect(self, item):
        self.calls.append(item.product_id)
        return replace(item, listing_status=self.statuses[item.product_id], price=120_000)


class SaleStatusTrackingTests(unittest.TestCase):
    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()

    def tearDown(self):
        self.database.close()

    def test_only_due_qualified_active_listing_is_checked_and_sold_price_is_preserved(self):
        due = active_listing("due")
        fresh = active_listing("fresh")
        self.assertTrue(self.database.add(due))
        self.assertTrue(self.database.add(fresh))
        now = datetime.now(UTC)
        old = (now - timedelta(hours=7)).isoformat().replace("+00:00", "Z")
        current = now.isoformat().replace("+00:00", "Z")
        self.database.connection.execute(
            "UPDATE listings SET last_active_at = ? WHERE product_id = 'due'", (old,)
        )
        self.database.connection.execute(
            "UPDATE listings SET last_active_at = ? WHERE product_id = 'fresh'", (current,)
        )
        self.database.connection.commit()

        crawler = FakeCrawler({"due": "sold", "fresh": "active"})
        result = track_sale_statuses(self.database, crawler, TRACKING)

        self.assertEqual((result.due_listings, result.active_listings_checked), (1, 1))
        self.assertEqual(result.sold_transitions, 1)
        self.assertEqual(crawler.calls, ["due"])
        row = self.database.connection.execute(
            "SELECT listing_status, first_sold_seen_at, last_active_price FROM listings WHERE product_id = 'due'"
        ).fetchone()
        self.assertEqual(row["listing_status"], "sold")
        self.assertIsNotNone(row["first_sold_seen_at"])
        self.assertEqual(row["last_active_price"], 150_000)

    def test_reserved_or_unqualified_rows_are_never_selected(self):
        reserved = replace(active_listing("reserved"), listing_status="reserved")
        bundle = replace(active_listing("bundle"), ai_scope="bundle")
        self.database.add(reserved)
        self.database.add(bundle)

        self.assertEqual(self.database.sale_status_candidates(TRACKING), [])

