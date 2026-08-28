import unittest
from dataclasses import replace

from used_pc_finder.database import ListingDatabase
from used_pc_finder.incremental import process_source_candidates
from used_pc_finder.models import Listing


def listing(number: int) -> Listing:
    return Listing(
        f"RTX 4070 SUPER #{number}",
        500000,
        f"https://example.test/listing/{number}",
        "Haan-dong",
        "local",
        str(number),
    )


class IncrementalScanTests(unittest.TestCase):
    def setUp(self):
        self.database = ListingDatabase(":memory:")
        self.database.initialize()
        self.addCleanup(self.database.close)
        self.inspected_ids: list[str | None] = []

    def inspect(self, candidate: Listing) -> Listing:
        self.inspected_ids.append(candidate.listing_id)
        return replace(
            candidate,
            description="정상 작동하며 이상 없습니다.",
            condition_status="normal",
        )

    def add_known(self, *numbers: int) -> None:
        for number in numbers:
            self.assertTrue(self.database.add(listing(number)))

    def test_new_new_then_three_known_stops_at_duplicate_threshold(self):
        self.add_known(3, 4, 5)
        result = process_source_candidates(
            [listing(number) for number in (1, 2, 3, 4, 5, 6)],
            self.database,
            self.inspect,
            duplicate_threshold=3,
            newest_first=True,
        )

        self.assertEqual(self.inspected_ids, ["1", "2"])
        self.assertEqual([item.listing_id for item in result.new_listings], ["1", "2"])
        self.assertEqual(result.known_skipped, 3)
        self.assertEqual(result.consecutive_seen, 3)
        self.assertTrue(result.stopped_at_duplicate_threshold)
        self.assertFalse(self.database.is_known(listing(6)))

    def test_new_new_known_new_resets_counter_and_continues(self):
        self.add_known(3, 5)
        result = process_source_candidates(
            [listing(number) for number in (1, 2, 3, 4, 5)],
            self.database,
            self.inspect,
            duplicate_threshold=3,
            newest_first=True,
        )

        self.assertEqual(self.inspected_ids, ["1", "2", "4"])
        self.assertEqual([item.listing_id for item in result.new_listings], ["1", "2", "4"])
        self.assertEqual(result.known_skipped, 2)
        self.assertEqual(result.consecutive_seen, 1)
        self.assertFalse(result.stopped_at_duplicate_threshold)

    def test_unverified_source_never_stops_early(self):
        self.add_known(1, 2, 3)
        result = process_source_candidates(
            [listing(number) for number in (1, 2, 3, 4)],
            self.database,
            self.inspect,
            duplicate_threshold=3,
            newest_first=False,
        )

        self.assertEqual(self.inspected_ids, ["4"])
        self.assertFalse(result.stopped_at_duplicate_threshold)
        self.assertEqual([item.listing_id for item in result.new_listings], ["4"])
