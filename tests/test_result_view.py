"""驗證結果頁可離線閱讀、保留草稿狀態且安全呈現動態文字。"""

import html
from html import parser
import pathlib
import tempfile
import unittest

import result_view


class PageParser(parser.HTMLParser):
    """收集元素與文字，檢查結果頁的可讀內容及外部資源。"""

    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


class ResultViewTest(unittest.TestCase):

    def test_dynamic_content_is_escaped_and_page_has_no_external_resources(self):
        injected = '<script>alert("讀值")</script>'
        order = {"id": "WO-E0001", "event_id": "E0001", "priority": "緊急",
                 "problem": injected, "event_status": "resolved", "status": "draft",
                 "actions": ["檢查感測器接線", injected], "causes": ["量測待確認"],
                 "created_at": "2026-11-07 16:29:00"}
        event = {"id": "E0001", "level": 3, "start_time": order["created_at"],
                 "status": "resolved", "advice": {"title": injected}}
        summary = {"status": "finished", "active_events": 0,
                   "first_timestamp": "2026-11-07 16:10:00",
                   "last_timestamp": "2026-11-07 20:19:00",
                   "last_state": {"equipment_level": 0, "quality_level": 0,
                                  "label": "normal"}}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "results.html"
            result_view.write_results_page(path, summary, [event], [order])
            document = path.read_text(encoding="utf-8")
        page = PageParser()
        page.feed(document)
        self.assertNotIn("script", page.tags)
        self.assertNotIn("iframe", page.tags)
        self.assertIn(html.escape(injected, quote=True), document)
        self.assertIn("details", page.tags)
        self.assertIn(("href", "#WO-E0001"), page.attributes)
        self.assertIn(("id", "WO-E0001"), page.attributes)
        self.assertFalse(any(name == "src" or (name == "href" and not value.startswith("#"))
                             for name, value in page.attributes))
        self.assertIn("工單：草稿待確認", "".join(page.text))
        self.assertIn("告警：已恢復", "".join(page.text))
        self.assertIn("告警恢復不代表維修完成", "".join(page.text))
        self.assertEqual(order["status"], "draft")

    def test_empty_incomplete_run_does_not_claim_equipment_is_normal(self):
        summary = {"status": "incomplete", "active_events": 0,
                   "last_timestamp": None, "last_state": None}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "results.html"
            result_view.write_results_page(path, summary, [], [])
            document = path.read_text(encoding="utf-8")
        self.assertIn("資料未完整讀取", document)
        self.assertIn("設備狀態待確認", document)
        self.assertIn("尚無事件紀錄", document)
        self.assertNotIn("<dd>正常</dd>", document)


if __name__ == "__main__":
    unittest.main()
