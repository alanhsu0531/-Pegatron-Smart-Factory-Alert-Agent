"""驗證三項監測後功能、確認儲存邊界與原始監測紀錄保護。"""

import io
import json
import pathlib
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock

from rich import console

import chat
import history_store
import main


CSV = pathlib.Path(__file__).resolve().parents[1] / "data/sensor_data.csv"


def call(name, arguments):
    """模擬 Ollama 工具呼叫，不實際連線。"""
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": arguments}}]}}


class PostMonitorChatTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.base = pathlib.Path(cls.fixture.name)
        main.run(CSV, cls.base, 0, console.Console(file=io.StringIO()))

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.directory.name)
        for name in ("summary.json", "events.json", "work_orders.json", "readings.jsonl"):
            shutil.copyfile(self.base / name, self.root / name)
        self.store = history_store.HistoryStore(self.root)
        self.run_id = self.store.runs()[0]["run_id"]
        self.session = chat.ChatSession(self.store)
        self.session.client.checked = True

    def tearDown(self):
        self.directory.cleanup()

    def proposal(self, note="人員回報：現場溫度正常"):
        return self.store.propose(self.run_id, "WO-E0005", 0, {"notes": note}, "加入現場備註")

    def test_history_preserves_early_warning_timeline_and_scopes_quality_events(self):
        result = self.store.query(keyword="E0005")
        times = {item["milestone"]: item["timestamp"] for item in result["timeline"]}
        self.assertTrue(times["first_model_warning"].endswith("17:00:00"))
        self.assertTrue(times["first_strict"].endswith("17:09:00"))
        self.assertEqual(result["model_warning_lead_minutes"], 9)
        quality = self.store.query(keyword="E0004")
        self.assertFalse(quality["timeline"])
        self.assertEqual(self.store.query(keyword="WO-E0005")["matching_orders"], 1)
        self.assertEqual(self.store.query(keyword='E0005”, ')["matching_events"], 1)
        self.assertEqual(self.store.query(keyword='wo-e0005 的備註')["matching_orders"], 1)
        self.assertEqual(self.store.query(keyword="不存在的事件")["matching_events"], 0)

    def test_handoff_counts_resolved_alerts_and_draft_orders_separately(self):
        result = self.store.handoff()
        self.assertEqual(result["event_counts"], {"關注": 10, "警告": 3, "危急": 2})
        self.assertEqual(result["resolved"], 15)
        self.assertEqual(len(result["pending_orders"]), 5)
        self.assertIn("不代表維修完成", result["note"])

    def test_confirmed_revision_preserves_base_and_rejects_stale_or_protected_changes(self):
        original = self.store.load(self.run_id)
        first, stale = self.proposal(), self.proposal("另一份尚未確認的備註")
        self.assertNotIn("operator_notes", original["orders"][1])
        saved = self.store.save(first)
        self.assertEqual(saved["version"], 1)
        with self.assertRaises(ValueError):
            self.store.save(stale)
        revised = self.store.load(self.run_id)
        order = next(item for item in revised["orders"] if item["id"] == "WO-E0005")
        self.assertEqual(order["operator_notes"], first["changes"]["notes"])
        self.assertEqual(order["status"], "draft")
        self.assertEqual(order["priority"], "緊急")
        self.assertEqual(original["events"], revised["events"])
        self.assertEqual(original["readings"], revised["readings"])
        with self.store.database() as connection:
            snapshot = json.loads(connection.execute("SELECT snapshot FROM runs WHERE id=?", (self.run_id,)).fetchone()[0])
        self.assertTrue(all("operator_notes" not in item for item in snapshot["orders"]))
        for field in ("priority", "status", "evidence", "equipment_level"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.propose(self.run_id, "WO-E0005", 1, {field: "正常"}, "禁止修改")

    def test_replay_retains_older_revision_and_latest_run_has_new_orders(self):
        self.store.save(self.proposal())
        main.run(CSV, self.root, 0, console.Console(file=io.StringIO()))
        self.assertEqual(len(self.store.runs()), 2)
        self.assertNotEqual(self.store.runs()[0]["run_id"], self.run_id)
        old_order = next(item for item in self.store.load(self.run_id)["orders"] if item["id"] == "WO-E0005")
        latest_order = next(item for item in self.store.load()["orders"] if item["id"] == "WO-E0005")
        self.assertEqual(old_order["revision"], 1)
        self.assertEqual(latest_order["revision"], 0)

    def test_read_answer_requires_a_tool_and_valid_record_reference(self):
        reply = {"answer": "溫度事件先出現模型警告，之後才嚴格超標。",
                 "references": [self.run_id + "/E0005"]}
        replies = [call("query_history", {"keyword": "E0005", "purpose": "查詢"}),
                   {"message": {"content": json.dumps(reply), "role": "assistant"}}]
        with mock.patch.object(self.session.client, "request", side_effect=replies):
            result = self.session.ask("查詢溫度事件")
        self.assertIn("E0005", result["sources"][0])
        reply["references"] = ["unknown/event"]
        replies[-1]["message"]["content"] = json.dumps(reply)
        with mock.patch.object(self.session.client, "request", side_effect=replies), self.assertRaises(ValueError):
            self.session.ask("查詢溫度事件")

    def test_previous_run_is_selected_even_if_model_supplies_latest_id(self):
        self.store.save(self.proposal())
        main.run(CSV, self.root, 0, console.Console(file=io.StringIO()))
        latest = self.store.runs()[0]["run_id"]
        reply = {"answer": "前一次執行的工單為第 1 版，備註為現場溫度正常。",
                 "references": [self.run_id + "/WO-E0005"]}
        replies = [call("query_history", {"run_id": latest, "keyword": "WO-E0005", "purpose": "查詢"}),
                   {"message": {"role": "assistant", "content": json.dumps(reply)}}]
        with mock.patch.object(self.session.client, "request", side_effect=replies):
            result = self.session.ask("查詢前一次執行的 WO-E0005")
        self.assertIn("第 1 版", result["sources"][0])

    def test_edit_tool_only_prepares_preview_and_requires_the_queried_version(self):
        arguments = {"run_id": self.run_id, "work_order_id": "WO-E0005", "expected_revision": 0,
                     "description": "先核對現場溫度，再依結果確認冷卻或感測器。", "notes": "現場溫度正常"}
        replies = [call("query_history", {"keyword": "WO-E0005", "purpose": "修改草稿"}),
                   call("propose_work_order_edit", arguments)]
        with mock.patch.object(self.session.client, "request", side_effect=replies):
            result = self.session.ask("修改溫度工單")
        self.assertEqual(result["proposal"]["changes"]["notes"], "現場溫度正常")
        order = next(item for item in self.store.load()["orders"] if item["id"] == "WO-E0005")
        self.assertEqual(order["revision"], 0)
        arguments["expected_revision"] = 1
        with mock.patch.object(self.session.client, "request", side_effect=replies), self.assertRaises(ValueError):
            self.session.ask("修改溫度工單")

    def test_edit_without_instructions_requests_details_before_a_proposal(self):
        replies = [call("query_history", {"keyword": "WO-E0005", "purpose": "修改草稿"}),
                   {"message": {"role": "assistant", "content": (
                       "1. 工單 ID：WO-E0005，問題摘要：溫度不在正常範圍  \n"
                       "2. 原始問題：溫度感測器讀數異常，連續超標")}}]
        with mock.patch.object(self.session.client, "request", side_effect=replies) as request:
            result = self.session.ask("修改 WO-E0005")
        self.assertIn("想修改", result["answer"])
        self.assertIn("WO-E0005", result["answer"])
        self.assertNotIn("proposal", result)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(next(order for order in self.store.load()["orders"]
                              if order["id"] == "WO-E0005")["revision"], 0)

    def test_plain_text_in_edit_tool_phase_retries_the_preview_tool(self):
        arguments = {"run_id": self.run_id, "work_order_id": "WO-E0005", "expected_revision": 0,
                     "description": "先核對現場溫度，再確認冷卻與感測器。"}
        replies = [call("query_history", {"keyword": "WO-E0005", "purpose": "修改草稿"}),
                   {"message": {"role": "assistant", "content": "1. 先核對現場溫度。"}},
                   call("propose_work_order_edit", arguments)]
        with mock.patch.object(self.session.client, "request", side_effect=replies):
            result = self.session.ask("請將 WO-E0005 的工單說明改寫得更簡短")
        self.assertEqual(result["proposal"]["changes"]["description"], arguments["description"])
        self.assertEqual(next(order for order in self.store.load()["orders"]
                              if order["id"] == "WO-E0005")["revision"], 0)

    def test_missing_edit_tool_stops_after_three_requests_without_a_revision(self):
        bad = {"message": {"role": "assistant", "content": "1. 先核對現場溫度。"}}
        replies = [call("query_history", {"keyword": "WO-E0005", "purpose": "修改草稿"}), bad, bad]
        with mock.patch.object(self.session.client, "request", side_effect=replies) as request, (
                self.assertRaisesRegex(ValueError, "尚未取得工單修改預覽")):
            self.session.ask("把 WO-E0005 的工單說明改寫得更簡短")
        self.assertEqual(request.call_count, 3)
        self.assertEqual(next(order for order in self.store.load()["orders"]
                              if order["id"] == "WO-E0005")["revision"], 0)

    def test_invalid_answer_json_is_retried_with_the_same_record_sources(self):
        reply = {"answer": "溫度工單仍為草稿待確認。", "references": [self.run_id + "/WO-E0005"]}
        replies = [call("query_history", {"keyword": "WO-E0005", "purpose": "查詢"}),
                   {"message": {"role": "assistant", "content": "1. 工單仍為草稿。"}},
                   {"message": {"role": "assistant", "content": json.dumps(reply)}}]
        with mock.patch.object(self.session.client, "request", side_effect=replies) as request:
            result = self.session.ask("查詢 WO-E0005")
        self.assertEqual(result["answer"], reply["answer"])
        self.assertEqual(request.call_count, 3)
        self.assertIn("format", request.call_args.args[1])

    def test_invalid_answer_json_exhaustion_has_a_friendly_error(self):
        bad = {"message": {"role": "assistant", "content": "1. 工單仍為草稿。"}}
        replies = [call("query_history", {"keyword": "WO-E0005", "purpose": "查詢"}), bad, bad]
        with mock.patch.object(self.session.client, "request", side_effect=replies), (
                self.assertRaisesRegex(ValueError, "模型回覆格式不符")):
            self.session.ask("查詢 WO-E0005")

    def test_unapproved_tool_cannot_save_or_run_commands(self):
        for name in ("save", "run_shell", "write_file"):
            with self.subTest(name=name), (
                    mock.patch.object(self.session.client, "request", return_value=call(name, {}))), (
                    self.assertRaises(ValueError)):
                self.session.ask("修改工單")

    def test_no_match_does_not_answer_from_old_dialogue(self):
        self.session.dialogue = [{"role": "assistant", "content": "之前說過現場溫度正常。"}]
        with mock.patch.object(self.session.client, "request", return_value=call(
                "query_history", {"purpose": "查詢", "keyword": "E9999"})) as request:
            result = self.session.ask("查詢 E9999 的備註")
        self.assertIn("找不到", result["answer"])
        self.assertNotIn("現場溫度正常", result["answer"])
        self.assertEqual(request.call_count, 1)

    def test_answer_and_search_use_traditional_chinese(self):
        reply = {"answer": "关注温度事件，工单仍待确认。", "references": [self.run_id + "/history"]}
        replies = [call("query_history", {"keyword": "温度", "purpose": "查詢"}),
                   {"message": {"role": "assistant", "content": json.dumps(reply)}}]
        with mock.patch.object(self.session.client, "request", side_effect=replies):
            result = self.session.ask("查詢溫度事件")
        self.assertEqual(result["answer"], "關注溫度事件，工單仍待確認。")

    def test_cancel_exit_and_save_use_explicit_confirmation(self):
        for decision in ("取消", "離開", "儲存"):
            output = io.StringIO()
            inputs = iter(["修改工單", decision, "離開"])
            with self.subTest(decision=decision), (
                    mock.patch.object(chat.ChatSession, "ask", return_value={"proposal": self.proposal()})):
                chat.run_chat(self.store, console.Console(file=output), lambda: next(inputs))
            order = next(item for item in self.store.load()["orders"] if item["id"] == "WO-E0005")
            self.assertEqual(order["revision"], 1 if decision == "儲存" else 0)
            self.assertIn("修改後", output.getvalue())
            self.assertNotIn("…", output.getvalue())

    def test_saved_page_escapes_notes_and_export_failure_does_not_lose_revision(self):
        saved = self.store.save(self.proposal('<script>alert("現場")</script>'))
        document = pathlib.Path(saved["path"]).read_text()
        self.assertNotIn('<script>', document)
        self.assertIn("&lt;script&gt;", document)
        self.assertIn("修訂第 1 版", document)
        proposal = self.store.propose(self.run_id, "WO-E0005", 1, {"notes": "再次回報"}, "修改備註")
        with mock.patch("history_store.result_view.write_results_page", side_effect=OSError("read-only")):
            saved = self.store.save(proposal)
        self.assertIsNone(saved["path"])
        self.assertEqual(next(item for item in self.store.load()["orders"] if item["id"] == "WO-E0005")["revision"], 2)

    def test_automatic_chat_only_starts_after_completed_interactive_monitoring(self):
        for status, terminal, disabled in (("finished", True, False), ("interrupted", True, False),
                                            ("finished", False, False), ("finished", True, True)):
            args = ["main.py", "--output-dir", str(self.root)] + (["--no-ollama"] if disabled else [])
            screen = console.Console(file=io.StringIO(), force_terminal=terminal)
            with self.subTest(status=status, terminal=terminal, disabled=disabled), (
                    mock.patch("sys.argv", args)), (
                    mock.patch("main.console.Console", return_value=screen)), (
                    mock.patch("main.sys.stdin.isatty", return_value=terminal)), (
                    mock.patch("main.run", return_value={"status": status})), (
                    mock.patch("main.chat.run_chat")) as start:
                main.main()
            self.assertEqual(start.call_count, int(status == "finished" and terminal and not disabled))

    def test_unavailable_model_keeps_records_and_gives_a_clear_message(self):
        original = self.store.load()
        output = io.StringIO()
        questions = iter(["交班摘要", "離開"])
        with mock.patch.object(chat.ChatSession, "ask", side_effect=urllib.error.URLError("connection refused")):
            chat.run_chat(self.store, console.Console(file=output), lambda: next(questions))
        self.assertIn("請開啟 Ollama", output.getvalue())
        self.assertNotIn("urlopen error", output.getvalue())
        self.assertEqual(original, self.store.load())


if __name__ == "__main__":
    unittest.main()
