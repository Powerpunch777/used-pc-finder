import unittest

from used_pc_finder.cli import BackfillProgressReporter


class ProgressReporterTests(unittest.TestCase):
    def test_reports_required_metrics_and_smoothed_then_precise_eta(self):
        now = [0.0]
        lines: list[str] = []
        reporter = BackfillProgressReporter(
            4, emit=lines.append, interval_seconds=0, clock=lambda: now[0]
        )
        reporter.report(
            completed_queries=0, pages_scanned=1, listings_inspected=10,
            valid_observations=2, excluded_listings=3, ai_queue_size=4,
            active_ai_workers=2, ai_completed=1, ai_failures=0, concurrency=4,
            crawl_status="crawling", force=True,
        )
        self.assertIn("estimated_remaining_seconds=calculating", lines[-1])
        self.assertIn("ai_queue_size=4", lines[-1])
        now[0] = 10.0
        reporter.report(
            completed_queries=1, pages_scanned=2, listings_inspected=20,
            valid_observations=3, excluded_listings=4, ai_queue_size=3,
            active_ai_workers=2, ai_completed=4, ai_failures=1, concurrency=4,
            crawl_status="crawling", force=True,
        )
        now[0] = 20.0
        reporter.report(
            completed_queries=1, pages_scanned=3, listings_inspected=30,
            valid_observations=4, excluded_listings=5, ai_queue_size=2,
            active_ai_workers=2, ai_completed=8, ai_failures=1, concurrency=4,
            crawl_status="crawling", force=True,
        )
        self.assertNotIn("estimated_remaining_seconds=calculating", lines[-1])

        reporter.add_ai_duration(2.0)
        reporter.add_ai_duration(4.0)
        reporter.report(
            completed_queries=4, pages_scanned=3, listings_inspected=30,
            valid_observations=4, excluded_listings=5, ai_queue_size=2,
            active_ai_workers=2, ai_completed=8, ai_failures=1, concurrency=2,
            crawl_status="crawl_complete_draining_ai", crawl_complete=True, force=True,
        )
        self.assertIn("rolling_avg_ai_seconds=3.000", lines[-1])
        self.assertIn("estimated_remaining_seconds=6.0", lines[-1])
