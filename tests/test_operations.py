from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from used_pc_finder.anomaly import assess_scan_anomalies
from used_pc_finder.backup import backup_database
from used_pc_finder.database import CandidateState, ListingDatabase
from used_pc_finder.models import Listing
from used_pc_finder.notifications import EmailNotifier, KakaoNotifier
from used_pc_finder.status import format_production_status, production_status


def listing(product_id: str = "1") -> Listing:
    return Listing(
        title="RTX 3060 Ti", price=200000, url=f"https://m.bunjang.co.kr/products/{product_id}",
        location="", source_type="test", marketplace="bunjang", product_id=product_id,
        listing_id=f"bunjang:{product_id}", updated_at="2026-01-01T00:00:00Z",
    )


class OperationsTests(unittest.TestCase):
    def test_ai_queue_recovers_stale_processing_and_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listings.sqlite3"
            with ListingDatabase(path) as database:
                database.initialize()
                item = listing()
                database.store_processed(item, CandidateState("new"))
                database.enqueue_ai_review(item, "fingerprint")
                self.assertTrue(database.mark_ai_review_processing(item))
                database.connection.execute("UPDATE ai_review_jobs SET started_at = '2000-01-01T00:00:00Z'")
                database.connection.commit()
                self.assertEqual(database.recover_stale_ai_reviews(1), 1)
                self.assertEqual([row.product_id for row in database.ready_ai_review_listings()], ["1"])
                database.finish_ai_review(item, error="rate limited")
                self.assertEqual(database.ai_review_queue_counts()["retry"], 1)

    def test_daily_backup_integrity_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "listings.sqlite3"
            with ListingDatabase(path) as database:
                database.initialize()
            backup_dir = root / "backups"
            created = backup_database(path, backup_dir, retain=1)
            self.assertIsNotNone(created)
            self.assertIsNone(backup_database(path, backup_dir, retain=1))
            self.assertTrue(created and created.exists())

    def test_anomaly_requires_independent_failure_evidence(self) -> None:
        movement_only = assess_scan_anomalies(
            price_change_percents=[80, -75, 70], search_records=100, valid_observations=50,
            prior_valid_observations=50, ai_candidates=10, ai_failures=0,
        )
        self.assertFalse(movement_only.safety_halt)
        self.assertEqual(movement_only.price_warnings, 3)
        coupled = assess_scan_anomalies(
            price_change_percents=[80, -75, 70], search_records=0, valid_observations=50,
            prior_valid_observations=50, ai_candidates=10, ai_failures=0,
        )
        self.assertTrue(coupled.safety_halt)

    def test_status_includes_queue_and_database_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "listings.sqlite3"
            with ListingDatabase(path) as database:
                database.initialize()
                data = production_status(database, runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()))
                text = format_production_status(data)
            self.assertIn("pending_ai=0", text)
            self.assertIn("listing_counts=", text)

    def test_notifier_boundary_keeps_email_and_future_kakao_separate(self) -> None:
        self.assertTrue(callable(EmailNotifier.send_digest))
        with self.assertRaises(NotImplementedError):
            KakaoNotifier().send_digest([], {})
