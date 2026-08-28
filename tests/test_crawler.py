import unittest
from unittest.mock import Mock

from used_pc_finder.legacy.karrot_crawler import PublicCrawler, extract_public_listings

HTML = """
<ul>
  <li><a href="/kr/buy-sell/rtx-card-abc123">
    <h2>RTX 4070 Super 팝니다</h2><span class="price">520,000원</span>
    <span class="location">광명시 하안동</span>
  </a></li>
  <li><a href="/kr/buy-sell/ipad-def456">
    <h2>아이패드</h2><span class="price">300,000원</span>
  </a></li>
</ul>
"""

LOCATION_SEPARATOR_HTML = """
<li><a href="/kr/buy-sell/rtx-card-abc123">
  <h2>RTX 4070 Super</h2><span class="price">520,000원</span>
  <span class="location">하안동 ·</span>
</a></li>
"""

CURRENT_STYLE_HTML = """
<a href="/kr/buy-sell/s/?search=4070"><span>4070</span></a>
<a data-gtm="search_article" href="/kr/buy-sell/rtx-card-xyz789/">
  <span>RTX 4070 Super 그래픽카드</span><span>520,000원</span>
  <span>하안동</span><span>·</span>
</a>
"""

BROKEN_DETAIL_HTML = """
<html><head><meta name="description" content="갑자기 작동 안됨. 부품용 고장품입니다."></head></html>
"""


class CrawlerTests(unittest.TestCase):
    def test_extracts_bounded_visible_fields(self):
        items = extract_public_listings(
            HTML, "https://www.daangn.com/kr/", "local", "Gwangmyeong", 1
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "RTX 4070 Super 팝니다")
        self.assertEqual(items[0].price, 520000)
        self.assertEqual(items[0].location, "광명시 하안동")
        self.assertEqual(items[0].url, "https://www.daangn.com/kr/buy-sell/rtx-card-abc123")

    def test_delay_occurs_between_requests(self):
        sleeper = Mock()
        crawler = PublicCrawler(delay_seconds=2, sleep=sleeper)
        response = Mock(text=HTML)
        response.raise_for_status = Mock()
        crawler.session.get = Mock(return_value=response)
        crawler.fetch("https://example/one")
        crawler.fetch("https://example/two")
        sleeper.assert_called_once_with(2)

    def test_extracts_current_flat_card_markup(self):
        item = extract_public_listings(
            CURRENT_STYLE_HTML, "https://www.daangn.com/kr/", "local", limit=1
        )[0]
        self.assertEqual(item.title, "RTX 4070 Super 그래픽카드")
        self.assertEqual(item.price, 520000)
        self.assertEqual(item.location, "하안동")

    def test_removes_the_public_page_location_separator(self):
        item = extract_public_listings(
            LOCATION_SEPARATOR_HTML, "https://www.daangn.com/kr/", "local", limit=1
        )[0]
        self.assertEqual(item.location, "하안동")

    def test_scan_reads_detail_description_and_classifies_condition(self):
        crawler = PublicCrawler(delay_seconds=0)
        index_response = Mock(text=HTML, encoding=None)
        detail_response = Mock(text=BROKEN_DETAIL_HTML, encoding=None)
        index_response.raise_for_status = Mock()
        detail_response.raise_for_status = Mock()
        crawler.session.get = Mock(side_effect=[index_response, detail_response])

        item = crawler.scan("https://www.daangn.com/kr/", "local", limit=1)[0]

        self.assertEqual(item.description, "갑자기 작동 안됨. 부품용 고장품입니다.")
        self.assertEqual(item.condition_status, "broken")
        self.assertEqual(crawler.session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
