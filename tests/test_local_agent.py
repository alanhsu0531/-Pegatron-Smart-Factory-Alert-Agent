"""驗證地端工具邊界、模型回覆及背景套用，不依賴實際 Ollama。"""

import copy
import datetime
import hashlib
import io
import json
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from rich import console

import main
from factory_agent import local_agent
from factory_agent import monitor


CSV = pathlib.Path(__file__).resolve().parents[1] / "data/sensor_data.csv"


def sample():
    """建立已由規則判定的危急事件與同時點歷史。"""
    reading = {
        "row": 270, "timestamp": "2026-11-07 16:29:00",
        "raw": {"temp": 56.0, "pressure": 1.15, "vibration": 0.12},
        "raw_valid": {sensor: True for sensor in monitor.SENSORS},
        "imputed": [], "quality": [], "label": "abnormal", "equipment_level": 3,
        "quality_level": 0, "score": 100, "score_provisional": False,
        "signals": {"equipment": {
            "kind": "multisensor", "level": 3,
            "sensors": list(monitor.SENSORS), "reasons": ["temp 56.0 嚴格超標"],
        }},
    }
    history = [{"row": 270, "timestamp": datetime.datetime(2026, 11, 7, 16, 29),
                **reading["raw"], "label": "abnormal"}]
    tracker = monitor.EventTracker()
    tracker.update(reading)
    event = tracker.events[0]
    return tracker, reading, history, local_agent.context_snapshot(event, reading, history)


def selection(context):
    """回傳有情境與查核分流的測試回覆，保留檢查動作。"""
    rules = context["get_playbook"]["follow_up"]
    branches = {branch: f"檢查{rule['targets'][0]}是否與現場狀態一致"
                for branch, rule in rules.items()}
    if context["get_event"]["kind"] in ("missing", "invalid"):
        branches["if_confirmed"] = "通知現場負責人" + branches["if_confirmed"]
    return {"event_id": context["get_event"]["event_id"],
            "cause_order": ["C2", "C1"], "evidence_refs": ["F1"],
            "action_refs": ["A1", "A2", "A3"],
            "situation": context["get_event"]["observations"]["summary"],
            "inspection_reason": "用來區分設備實際變化與量測回報問題。",
            "next_step": branches}


def tool_message(names=local_agent.TOOL_NAMES):
    """模擬 Ollama 的平行工具呼叫格式。"""
    return {"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": name, "arguments": {}}} for name in names]}}


class LocalAgentTest(unittest.TestCase):

    def test_snapshot_rejects_future_and_does_not_share_mutable_history(self):
        _, reading, history, context = sample()
        history[0]["temp"] = 999
        self.assertEqual(context["get_history"][0]["temp"], 56)
        history[0]["row"] = 271
        with self.assertRaises(ValueError):
            local_agent.context_snapshot(sample()[0].events[0], reading, history)

    def test_schema_and_validation_allow_only_current_evidence_and_known_causes(self):
        _, _, _, context = sample()
        valid = selection(context)
        result = local_agent.validate_result(valid, context)
        self.assertEqual(result["source"], "Ollama 地端模型")
        self.assertEqual(result["causes"][0], context["get_playbook"]["causes"]["C2"])
        self.assertEqual(result["actions"], context["get_playbook"]["actions"])
        self.assertTrue(result["inspection_reason"].startswith(result["actions"][0]))
        self.assertIn(result["next_step"], result["recommendation"])
        bad = [dict(valid, event_id="E9999"), dict(valid, cause_order=["C1", "C1"]),
               dict(valid, evidence_refs=["F999"]), dict(valid, evidence_refs=["F1", "F1"]),
               dict(valid, risk="正常"), dict(valid, actions=["更換軸承"]),
               dict(valid, cause_order=[[], "C1"])]
        for result in bad:
            with self.subTest(result=result), self.assertRaises(ValueError):
                local_agent.validate_result(result, context)
        schema = local_agent.output_schema(context)
        self.assertFalse(schema["additionalProperties"])

    def test_explanation_rejects_unsupported_actions_claims_and_missing_branches(self):
        _, _, _, context = sample()
        valid = selection(context)
        bad = [dict(valid, action_refs=["A999"]), dict(valid, action_refs=["A2"]),
               dict(valid, next_step={"if_confirmed": "通知維修人員立即更換軸承並重啟設備",
                                      "if_not_confirmed": "確認感測器固定位置與回報通道"}),
               dict(valid, next_step="請核對現場讀值，再安排維修人員查核。"),
               dict(valid, next_step={"if_confirmed": "現場量測與感測器讀值一致",
                                      "if_not_confirmed": "確認感測器固定位置與回報通道"}),
               dict(valid, next_step={"if_confirmed": "通知現場負責人查核負載與冷卻狀態"}),
               dict(valid, situation="溫度已達五十六度，故障機率為百分之九十。"),
               dict(valid, situation="目前溫度為 56 度，請先確認異常。"),
               dict(valid, situation="AOI回報正常，請核對現場設備狀況。"),
               dict(valid, situation="溫度持續上升，壓力與振動也需要確認。"),
               dict(valid, inspection_reason="此處已確認故障，請通知現場人員確認。")]
        for result in bad:
            with self.subTest(result=result), self.assertRaises(ValueError):
                local_agent.validate_result(result, context)

    def test_branch_targets_cannot_reverse_equipment_and_measurement_checks(self):
        _, reading, history, _ = sample()
        reading["signals"]["equipment"].update(kind="thermal", sensors=["temp"])
        tracker = monitor.EventTracker()
        tracker.update(reading)
        context = local_agent.context_snapshot(tracker.events[0], reading, history)
        valid = selection(context)
        invalid = dict(valid, next_step={"if_confirmed": "檢查感測器固定位置是否牢固",
                                         "if_not_confirmed": "觀察馬達負載與冷卻狀態"})
        with self.assertRaises(ValueError):
            local_agent.validate_result(invalid, context)
        result = local_agent.validate_result(valid, context)
        self.assertIn("若現場確認相同異常", result["next_step"])

    def test_quality_branch_notifies_owner_without_diagnosing_equipment(self):
        _, reading, history, _ = sample()
        reading["raw"]["pressure"] = 999
        reading["raw_valid"]["pressure"] = False
        reading["equipment_level"] = None
        reading["signals"] = {"quality:invalid:pressure": {
            "kind": "invalid", "level": 2, "sensors": ["pressure"],
            "reasons": ["pressure 回傳無效數值"],
        }}
        tracker = monitor.EventTracker()
        tracker.update(reading)
        context = local_agent.context_snapshot(tracker.events[0], reading, history)
        candidate = selection(context)
        candidate["next_step"]["if_confirmed"] = "檢查接線與現場儀表是否一致"
        result = local_agent.validate_result(candidate, context)
        self.assertIn("若現場量測也有異常，通知現場負責人", result["next_step"])
        self.assertIn("若現場量測正常", result["next_step"])
        self.assertIsNone(reading["equipment_level"])

    def test_contextual_text_reaches_terminal_draft_and_resets_on_event_change(self):
        tracker, reading, history, context = sample()
        result = local_agent.validate_result(selection(context), context)
        tracker.set_advice("E0001", result)
        tracker.update(copy.deepcopy(reading))
        order = tracker.work_orders["E0001"]
        for key in ("situation", "inspection_reason", "next_step", "as_of"):
            self.assertEqual(order[key], result[key])
        for height in (24, 36):
            with self.subTest(height=height):
                rendered = io.StringIO()
                console.Console(file=rendered, width=80).print(
                    main.dashboard(reading, tracker, 80, history, height))
                self.assertLessEqual(len(rendered.getvalue().splitlines()), height)
                self.assertIn("若現場", rendered.getvalue())
                self.assertIn("16:29", rendered.getvalue())
        changed = copy.deepcopy(reading)
        changed["signals"]["equipment"].update(kind="thermal", sensors=["temp"])
        tracker.update(changed)
        self.assertNotIn("situation", tracker.work_orders["E0001"])
        self.assertEqual(tracker.work_orders["E0001"]["source"], "本機規則模板")

    def test_client_verifies_local_tool_model_before_upload(self):
        _, _, _, context = sample()
        client = local_agent.OllamaClient()
        final = {"message": {"role": "assistant", "content": json.dumps(selection(context))}}
        replies = [{"models": [{"name": local_agent.MODEL, "size": 2500000000}]},
                   {"capabilities": ["tools", "completion"]}, tool_message(), final]
        with mock.patch.object(client, "request", side_effect=replies) as request:
            result = client.explain(context, time.monotonic() + 30)
        self.assertEqual(result["trace"]["status"], "accepted")
        self.assertEqual(result["trace"]["requests"], 2)
        self.assertEqual(len(result["trace"]["tools"]), 3)
        self.assertEqual([call.args[0] for call in request.call_args_list[:2]],
                         ["/api/tags", "/api/show"])
        payload = request.call_args_list[-1].args[1]
        self.assertIn("format", payload)
        self.assertNotIn("tools", payload)
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])

    def test_cloud_feature_switch_does_not_block_local_model(self):
        for disabled in (False, True):
            replies = {"/api/status": {"cloud": {"disabled": disabled}},
                       "/api/tags": {"models": [{"name": local_agent.MODEL, "size": 1}]},
                       "/api/show": {"capabilities": ["tools", "completion"]}}
            client = local_agent.OllamaClient()
            with self.subTest(disabled=disabled), mock.patch.object(
                    client, "request", side_effect=lambda path, payload, deadline: replies[path]) as request:
                client.check_local(time.monotonic() + 3)
            self.assertTrue(client.checked)
            self.assertNotIn("/api/status", [call.args[0] for call in request.call_args_list])

    def test_invalid_explanation_can_be_repaired_within_three_model_requests(self):
        _, _, _, context = sample()
        client = local_agent.OllamaClient()
        client.checked = True
        bad = selection(context)
        bad["next_step"] = {"if_confirmed": "現場量測與感測器回報一致",
                            "if_not_confirmed": "確認感測器固定位置與讀值"}
        replies = [tool_message(),
                   {"message": {"role": "assistant", "content": json.dumps(bad)}},
                   {"message": {"role": "assistant", "content": json.dumps(selection(context))}}]
        with mock.patch.object(client, "request", side_effect=replies):
            result = client.explain(context, time.monotonic() + 30)
        self.assertIsNotNone(result["advice"])
        self.assertEqual(result["trace"]["requests"], 3)
        self.assertEqual(len(result["trace"]["validation_errors"]), 1)

    def test_missing_remote_or_unsupported_model_is_rejected_without_event_upload(self):
        _, _, _, context = sample()
        for replies in ([{"models": []}],
                        [{"models": [{"name": local_agent.MODEL, "size": 1}]},
                         {"remote_host": "example.com", "capabilities": ["tools"]}],
                        [{"models": [{"name": local_agent.MODEL, "size": 1}]},
                         {"capabilities": ["completion"]}]):
            with self.subTest(replies=replies):
                client = local_agent.OllamaClient()
                with mock.patch.object(client, "request", side_effect=replies) as request:
                    result = client.explain(context, time.monotonic() + 30)
                self.assertIsNone(result["advice"])
                self.assertEqual(result["trace"]["requests"], 0)
                self.assertNotIn("/api/chat", [call.args[0] for call in request.call_args_list])

    def test_bad_tools_missing_tools_limits_and_timeout_fall_back(self):
        _, _, _, context = sample()
        invalid_tool = tool_message(["run_shell"])
        invalid_args = tool_message(["get_event"])
        invalid_args["message"]["tool_calls"][0]["function"]["arguments"] = {"row": 500}
        missing_tools = {"message": {"role": "assistant", "content": "{}"}}
        cases = [[invalid_tool], [invalid_args], [missing_tools],
                 [tool_message(["get_event"])] * 3,
                 [tool_message(["get_event"] * 5)], [TimeoutError("timeout")]]
        for replies in cases:
            client = local_agent.OllamaClient()
            client.checked = True
            with self.subTest(replies=replies), (
                    mock.patch.object(client, "request", side_effect=replies)):
                result = client.explain(context, time.monotonic() + 30)
            self.assertIsNone(result["advice"])
            self.assertEqual(result["trace"]["status"], "fallback")
            self.assertLessEqual(result["trace"]["requests"], 3)
            self.assertLessEqual(len(result["trace"]["tools"]), 4)

    def test_transport_uses_loopback_and_rejects_redirects_and_expired_deadline(self):
        client = local_agent.OllamaClient()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"models":[]}'
        with mock.patch.object(client.opener, "open", return_value=response) as opened:
            client.request("/api/tags", None, time.monotonic() + 1)
        self.assertEqual(opened.call_args.args[0].full_url,
                         "http://127.0.0.1:11434/api/tags")
        self.assertIsNone(local_agent.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://example.com"))
        with self.assertRaises(TimeoutError):
            client.request("/api/tags", None, time.monotonic() - 1)

    def test_background_does_not_block_and_stale_reply_cannot_replace_new_event(self):
        tracker, reading, history, context = sample()
        release = threading.Event()

        def explain(snapshot, deadline):
            release.wait(1)
            return {"advice": local_agent.validate_result(selection(snapshot), snapshot),
                    "trace": {"event_id": "E0001", "status": "accepted"}}

        trace = io.StringIO()
        with mock.patch.object(local_agent.OllamaClient, "explain", side_effect=explain):
            agent = local_agent.LocalAgent(trace)
            try:
                started = time.monotonic()
                agent.update(tracker, reading, history)
                self.assertLess(time.monotonic() - started, 0.2)
                agent.update(tracker, reading, history)
                self.assertEqual(len(agent.pending), 1)
                tracker.events[0]["kind"] = "thermal"
                release.set()
                agent.close(tracker)
            finally:
                release.set()
                agent.close(tracker, wait=False)
        self.assertEqual(json.loads(trace.getvalue())["status"], "superseded")
        self.assertEqual(tracker.events[0]["advice"]["source"], "本機規則模板")

    def test_accepted_advice_survives_next_reading_and_updates_same_draft(self):
        tracker, reading, _, context = sample()
        tracker.set_advice("E0001", local_agent.validate_result(selection(context), context))
        tracker.update(copy.deepcopy(reading))
        self.assertEqual(tracker.events[0]["advice"]["source"], "Ollama 地端模型")
        self.assertEqual(tracker.work_orders["E0001"]["source"], "Ollama 地端模型")
        self.assertEqual(tracker.work_orders["E0001"]["status"], "draft")
        self.assertEqual(tracker.events[0]["level"], 3)
        self.assertEqual(len(tracker.work_orders), 1)

    def test_deadline_and_worker_errors_cannot_stall_the_monitor(self):
        for failure in (False, True):
            tracker, reading, history, _ = sample()
            release = threading.Event()

            def explain(context, deadline):
                if failure:
                    raise RuntimeError("unexpected worker failure")
                release.wait(1)
                return {"advice": None, "trace": {"status": "fallback"}}

            with self.subTest(failure=failure), (
                    mock.patch.object(local_agent.OllamaClient, "explain", side_effect=explain)), (
                    mock.patch.object(local_agent, "TIMEOUT", 0.01)):
                trace = io.StringIO()
                agent = local_agent.LocalAgent(trace)
                try:
                    started = time.monotonic()
                    agent.update(tracker, reading, history)
                    agent.close(tracker)
                    self.assertLess(time.monotonic() - started, 0.5)
                    self.assertEqual(tracker.events[0]["advice"]["source"], "規則備援")
                    self.assertEqual(json.loads(trace.getvalue())["status"], "fallback")
                finally:
                    release.set()
                    agent.close(tracker, wait=False)

    def test_stop_does_not_wait_or_allow_a_late_reply_to_update_the_event(self):
        tracker, reading, history, context = sample()
        release, finished = threading.Event(), threading.Event()

        def explain(snapshot, deadline):
            release.wait(1)
            finished.set()
            return {"advice": local_agent.validate_result(selection(snapshot), snapshot),
                    "trace": {"status": "accepted"}}

        trace = io.StringIO()
        with mock.patch.object(local_agent.OllamaClient, "explain", side_effect=explain):
            agent = local_agent.LocalAgent(trace)
            try:
                agent.update(tracker, reading, history)
                started = time.monotonic()
                agent.close(tracker, wait=False)
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertEqual(json.loads(trace.getvalue())["status"], "cancelled")
                release.set()
                finished.wait(0.1)
                agent.poll(tracker)
                self.assertEqual(tracker.events[0]["advice"]["source"], "本機規則模板")
            finally:
                release.set()

    def test_dashboard_shows_current_advice_source_and_fits_small_windows(self):
        tracker, reading, history, context = sample()
        tracker.agent_status = "Ollama 地端模型"
        states = [(set(), "本機規則模板"), ({"E0001"}, "AI 整理中")]
        for pending, expected in states:
            tracker.agent_pending = pending
            for height in (24, 36):
                with self.subTest(pending=pending, height=height):
                    rendered = io.StringIO()
                    console.Console(file=rendered, width=80).print(
                        main.dashboard(reading, tracker, 80, history, height))
                    lines = rendered.getvalue().splitlines()
                    self.assertLessEqual(len(lines), height)
                    self.assertIn(expected, rendered.getvalue())

    def test_enabled_stream_preserves_risk_csv_and_metadata_boundary(self):
        before = hashlib.sha256(CSV.read_bytes()).hexdigest()
        original_open = pathlib.Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name == "generation_metadata.json":
                raise AssertionError("Agent 不可讀取生成答案")
            return original_open(path, *args, **kwargs)

        def explain(context, deadline):
            return {"advice": local_agent.validate_result(selection(context), context),
                    "trace": {"event_id": context["get_event"]["event_id"],
                              "status": "accepted"}}

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            output = console.Console(file=io.StringIO())
            template = main.run(CSV, root / "template", 0, output)
            with mock.patch.object(pathlib.Path, "open", guarded_open), (
                    mock.patch.object(local_agent.OllamaClient, "explain", side_effect=explain)):
                result = main.run(CSV, root / "agent", 0, output, use_ollama=True)
            self.assertEqual(result["equipment_level_counts"], template["equipment_level_counts"])
            self.assertEqual(result["work_orders"], template["work_orders"])
            self.assertGreater(result["agent"]["accepted"], 0)
            self.assertIn("優先工單建議", output.file.getvalue())
            orders = json.loads((root / "agent/work_orders.json").read_text())
            self.assertTrue(all(order["source"] == "Ollama 地端模型" for order in orders))
            self.assertTrue(all(order["situation"] and order["next_step"] for order in orders))
            page = (root / "agent/results.html").read_text()
            self.assertIn("Ollama 地端模型", page)
            self.assertIn("查核後處置", page)
            self.assertTrue(all(order["status"] == "draft" for order in orders))
        self.assertEqual(hashlib.sha256(CSV.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()
