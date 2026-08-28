import sqlite3
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
