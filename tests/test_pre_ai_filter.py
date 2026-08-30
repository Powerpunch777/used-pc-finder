import unittest

from used_pc_finder.models import Listing
from used_pc_finder.pre_ai_filter import cheap_listing_scope, deterministic_standalone_name


def listing(title: str, description: str = "") -> Listing:
    return Listing(
        title, 500000, "https://example.test/item", "Seoul", "test",
        description=description, condition_status="normal",
    )


class CompletePcScopeTests(unittest.TestCase):
    def test_dash_delimited_desktop_inventory_is_complete_pc(self):
        item = listing(
            "라이젠 7700X RTX4060TI 게이밍 데스크탑 판매합니다.",
            "CPU - RYZEN7 7700X\nRAM - SAMSUNG DDR5 32GB\n"
            "M.B - MSI X670E\nVGA - RTX 4060TI\nP.S - 700W",
        )
        self.assertEqual(cheap_listing_scope(item), "complete_pc")

    def test_slash_delimited_7800x3d_system_inventory_is_complete_pc(self):
        item = listing(
            "라이젠7 7800X3D RTX5070 화이트 감성본체",
            "라이젠7 7800X3D / B650M-K / DDR5 32GB / NVME M.2 1TB SSD / "
            "RTX5070 12GB / 850W 파워 / 어항케이스",
        )
        self.assertEqual(cheap_listing_scope(item), "complete_pc")

    def test_laptop_wanted_trade_and_box_titles_are_not_parts(self):
        # ``unknown`` is the fail-closed persisted scope for laptop and
        # wanted/trade offers; neither can become a price observation.
        self.assertEqual(cheap_listing_scope(listing("ASUS ROG RTX 4060 게이밍 노트북")), "unknown")
        self.assertEqual(cheap_listing_scope(listing("RTX4060 그래픽카드 삽니다")), "unknown")
        self.assertEqual(cheap_listing_scope(listing("컬러풀 RTX 4060 박스")), "accessory")

    def test_actual_laptop_and_complete_pc_patterns_are_excluded(self):
        laptop = listing(
            "ASUS TUF Gaming F16 i7-13650HX RTX 4060 16GB RAM 165hz",
            "ASUS TUF Gaming F16 노트북입니다. Windows 11 Home 설치.",
        )
        complete_pc = listing(
            "라이젠7800X3D RTX5060TI 고사양 컴퓨터",
            "사진상 사양 확인해주세요. 구입시기 25년 7월.",
        )
        os_equipped_system = listing(
            "AMD 라이젠7 9700X, ASUS RTX 4060",
            "사용감 없는 제품이며 윈도우11 정품도 탑재되어 있습니다.",
        )
        self.assertEqual(cheap_listing_scope(laptop), "unknown")
        self.assertEqual(cheap_listing_scope(complete_pc), "complete_pc")
        self.assertEqual(cheap_listing_scope(os_equipped_system), "complete_pc")

    def test_standalone_gpu_removed_from_pc_is_not_false_positive(self):
        item = listing(
            "RTX 4060 Ti 8GB MSI 벤투스 판매합니다",
            "정상 작동, 채굴 없음. 본체에서 분리한 그래픽카드 본품입니다.",
        )
        self.assertIsNone(cheap_listing_scope(item))
        self.assertEqual(deterministic_standalone_name(item), "RTX 4060 Ti")


if __name__ == "__main__":
    unittest.main()
