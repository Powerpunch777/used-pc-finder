import sqlite3
import tempfile
import unittest
from pathlib import Path

from used_pc_finder.ai_classifier import CLASSIFIER_VERSION
from used_pc_finder.database import ListingDatabase
from used_pc_finder.models import Listing


def listing(url="https://www.daangn.com/articles/1", listing_id="1"):
    return Listing(
        "4070s",
        520000,
        url,
        "Haan-dong",
        "local",
        listing_id,
        "정상 작동",
        "normal",
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()

    def tearDown(self):
        self.database.close()

    def test_duplicate_url_is_ignored(self):
        self.assertTrue(self.database.add(listing()))
        self.assertFalse(self.database.add(listing(listing_id="2")))
        self.assertEqual(self.database.count(), 1)

    def test_duplicate_listing_id_is_ignored(self):
        self.assertTrue(self.database.add(listing()))
        self.assertFalse(self.database.add(listing(url="https://www.daangn.com/articles/2")))
        self.assertEqual(self.database.count(), 1)

    def test_notification_state_is_persisted(self):
        item = listing()
        self.database.add(item)
        self.assertFalse(self.database.was_notified(item))
        self.database.mark_notified(item)
        self.assertTrue(self.database.was_notified(item))

    def test_condition_details_are_persisted(self):
        item = listing()
        self.database.add(item)
        row = self.database.connection.execute(
            "SELECT description, condition_status FROM listings WHERE url = ?", (item.url,)
        ).fetchone()
        self.assertEqual(row["description"], "정상 작동")
        self.assertEqual(row["condition_status"], "normal")

    def test_ai_details_are_persisted(self):
        item = Listing(
            "4070s",
            520000,
            "https://www.daangn.com/articles/ai",
            "Haan-dong",
            "local",
            "ai",
            "정상 작동",
            "normal",
            True,
            "RTX 4070 SUPER",
            0.95,
            False,
            "working GPU",
        )
        self.database.add(item)
        row = self.database.connection.execute(
            "SELECT ai_is_computer_part, ai_normalized_product_name, ai_confidence, ai_reject "
            "FROM listings WHERE url = ?",
            (item.url,),
        ).fetchone()
        self.assertEqual(row["ai_is_computer_part"], 1)
        self.assertEqual(row["ai_normalized_product_name"], "RTX 4070 SUPER")
        self.assertEqual(row["ai_confidence"], 0.95)
        self.assertEqual(row["ai_reject"], 0)

    def test_listing_id_or_url_marks_candidate_as_known(self):
        self.database.add(listing())
        self.assertTrue(self.database.is_known(listing()))
        self.assertTrue(
            self.database.is_known(listing(url="https://www.daangn.com/articles/other"))
        )
        self.assertTrue(
            self.database.is_known(listing(listing_id="other-id"))
        )

    def test_failed_ai_classification_remains_pending_for_retry(self):
        item = Listing(
            "RTX 4070 SUPER", 500000, "https://m.bunjang.co.kr/products/1", "", "bunjang_search",
            "bunjang:1", "정상 작동", "unknown", False, None, 0.0, True, "CLI timeout",
            marketplace="bunjang", product_id="1", updated_at="2026-08-28T00:00:00Z",
        )
        self.database.add(item)
        self.database.record_ai_classification(
            item,
            "same-content",
            model="gpt-5.6-luna",
            reasoning_effort="low",
            classifier_version=CLASSIFIER_VERSION,
            classification=None,
            execution_duration_seconds=60.0,
            error_reason="CLI timeout",
        )
        self.assertEqual(self.database.candidate_state(item).status, "pending_ai")

    def test_initialize_safely_migrates_a_legacy_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE listings (
                    id INTEGER PRIMARY KEY,
                    listing_id TEXT UNIQUE,
                    url TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    price INTEGER NOT NULL,
                    location TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                INSERT INTO listings (listing_id, url, title, price, location, source_type)
                VALUES ('legacy-id', 'https://example/legacy', 'legacy GPU', 1, '', 'local')
                """
            )
            connection.commit()
            connection.close()

            migrated = ListingDatabase(path)
            try:
                migrated.initialize()
                row = migrated.connection.execute(
                    "SELECT marketplace, product_id, condition_status FROM listings"
                ).fetchone()
            finally:
                migrated.close()

            self.assertEqual(row["marketplace"], "karrot")
            self.assertEqual(row["product_id"], "legacy-id")
            self.assertEqual(row["condition_status"], "unknown")

    def test_audit_invalidates_sold_complete_pc_and_bundle_observations_only(self):
        values = [
            ("part", "AMD Ryzen 5 5600X", "정상 작동 CPU", "active"),
            ("bundle", "Ryzen 5 5600X + B450 메인보드", "세트 판매", "active"),
            ("pc", "라이젠 5600X 본체", "완본체", "active"),
            ("sold", "AMD Ryzen 5 5600X", "정상 작동 CPU", "sold"),
        ]
        for product_id, title, description, status in values:
            item = Listing(
                title, 150_000, f"https://m.bunjang.co.kr/products/{product_id}", "",
                "bunjang_search", f"bunjang:{product_id}", description, "normal",
                marketplace="bunjang", product_id=product_id, listing_status=status,
            )
            self.assertTrue(self.database.add(item))
            self.database.connection.execute(
                """INSERT INTO price_observations
                   (marketplace, product_id, normalized_product_name, observed_price,
                    observed_at, first_seen_at, listing_id)
                   VALUES ('bunjang', ?, 'Ryzen 5 5600X', 150000, '2026-08-28T00:00:00Z',
                           '2026-08-28T00:00:00Z', ?)""",
                (product_id, f"bunjang:{product_id}"),
            )
        self.database.connection.commit()

        result = self.database.invalidate_contaminated_price_observations()

        self.assertEqual(result["invalidated"], 3)
        self.assertEqual(
            [item.product_id for item in self.database.price_observations("Ryzen 5 5600X")],
            ["part"],
        )


if __name__ == "__main__":
    unittest.main()
