"""驗證因果資料處理、門檻、事件生命週期及只讀的完整終端流程。"""

import datetime
import contextlib
import hashlib
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np
from rich import console
from rich import cells

import evaluate_model
import main
from factory_agent import monitor


ROOT = pathlib.Path(__file__).resolve().parents[1]
CSV = ROOT / "data" / "sensor_data.csv"


class MonitorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        records = monitor.read_records(CSV)
        cls.baseline = monitor.initial_records(records)
        records.close()

    def record(self, offset, values=(47.5, 1.02, 0.03), label="normal"):
        """建立初始化之後的單筆邊界案例，不載入未來樣本。"""
        return {"row": 250 + offset,
                "timestamp": self.baseline[-1]["timestamp"]
                + datetime.timedelta(minutes=offset), "label": label,
                **dict(zip(monitor.SENSORS, values))}

    def test_initialization_reads_exactly_250_and_model_excludes_label(self):
        visited = []

        def source():
            for index in range(500):
                visited.append(index + 1)
                yield self.baseline[index % 250]

        records = source()
        first = monitor.initial_records(records)
        self.assertEqual(visited, list(range(1, 251)))
        self.assertEqual(len(first), 250)
        records.close()
        detector = monitor.Monitor(self.baseline)
        changed_labels = [dict(row, label="abnormal") for row in self.baseline]
        other = monitor.Monitor(changed_labels)
        self.assertEqual(detector.baseline_counts,
                         {"initial": 250, "training": 200, "calibration": 50})
        sample = np.array([47.5, 1.02, 0.03])
        self.assertEqual(detector.score(sample), other.score(sample))
        fitted = detector.model.estimators_
        detector.analyze(self.record(1))
        self.assertIs(detector.model.estimators_, fitted)

    def test_strict_boundaries_and_immediate_critical(self):
        for values in [(43.0, 0.97, 0.03), (52.0, 1.08, 0.07)]:
            with self.subTest(values=values):
                detector = monitor.Monitor(self.baseline)
                with mock.patch.object(detector, "score", return_value=0.0):
                    reading = detector.analyze(self.record(1, values))
                self.assertEqual(reading["strict_sensors"], [])
                self.assertLess(reading["equipment_level"], 2)
        detector = monitor.Monitor(self.baseline)
        with mock.patch.object(detector, "score", return_value=0.0):
            reading = detector.analyze(self.record(1, (52.1, 1.09, 0.03)))
        self.assertEqual(reading["equipment_level"], 3)

    def test_persistence_joint_gray_and_aoi(self):
        cases = [((53.0, 1.02, 0.03), "normal", [2, 2, 3]),
                 ((51.0, 1.06, 0.06), "normal", [1, 1, 2]),
                 ((47.5, 1.02, 0.03), "abnormal", [1, 1, 2])]
        for values, label, levels in cases:
            with self.subTest(values=values, label=label):
                detector = monitor.Monitor(self.baseline)
                with mock.patch.object(detector, "score", return_value=0.0):
                    readings = [detector.analyze(self.record(i, values, label))
                                for i in range(1, 4)]
                self.assertEqual([r["equipment_level"] for r in readings], levels)

    def test_missing_values_stop_filling_after_two_minutes(self):
        detector = monitor.Monitor(self.baseline)
        previous = self.baseline[-1]["temp"]
        records = [self.record(i, (None, 1.02, 0.03)) for i in range(1, 4)]
        with mock.patch.object(detector, "score", return_value=100.0):
            readings = [detector.analyze(record) for record in records]
        for reading in readings[:2]:
            self.assertEqual(reading["effective"]["temp"], previous)
            self.assertTrue(reading["score_provisional"])
            self.assertIsNone(reading["raw"]["temp"])
            self.assertNotIn("equipment", reading["signals"])
        self.assertIsNone(readings[2]["effective"]["temp"])
        self.assertIsNone(readings[2]["score"])
        self.assertEqual(readings[2]["quality_level"], 2)
        self.assertTrue(all(record["temp"] is None for record in records))

    def test_no_prior_value_stays_unavailable_and_999_is_quality_only(self):
        detector = monitor.Monitor(self.baseline)
        detector.last_valid["temp"] = None
        with mock.patch.object(detector, "score", return_value=0.0):
            reading = detector.analyze(self.record(1, (None, 1.02, 0.03)))
        self.assertIsNone(reading["effective"]["temp"])
        self.assertIsNone(reading["equipment_level"])
        with mock.patch.object(detector, "score", return_value=100.0):
            invalid = detector.analyze(self.record(2, (47.5, 999, 0.03)))
        self.assertEqual(invalid["raw"]["pressure"], 999)
        self.assertEqual(invalid["quality_level"], 2)
        self.assertNotIn("pressure", invalid["strict_sensors"])
        self.assertNotIn("equipment", invalid["signals"])

    def test_repetition_is_diagnostic_only_and_imputation_resets_run(self):
        detector = monitor.Monitor(self.baseline)
        tracker = monitor.EventTracker()
        with mock.patch.object(detector, "score", return_value=0.0):
            for index in range(1, 23):
                reading = detector.analyze(self.record(index))
                tracker.update(reading)
                self.assertEqual(reading["quality_level"], 0)
                self.assertEqual(reading["quality"], [])
                self.assertEqual(reading["signals"], {})
                self.assertTrue(all(reading["raw_valid"].values()))
            self.assertTrue(all(count >= 22 for count in
                                reading["unchanged_samples"].values()))
            self.assertEqual(tracker.events, [])
            previous_counts = reading["unchanged_samples"]
            reading = detector.analyze(self.record(23, (47.5, 1.02, None)))
            self.assertIn("vibration", reading["imputed"])
            self.assertEqual(reading["unchanged_samples"]["vibration"], 0)
            self.assertGreaterEqual(previous_counts["vibration"], 22)
            reading = detector.analyze(self.record(24))
            self.assertEqual(reading["unchanged_samples"]["vibration"], 1)
        self.assertEqual(tracker.work_orders, {})

    def test_current_sensor_changes_without_losing_peak_risk(self):
        detector = monitor.Monitor(self.baseline)
        tracker = monitor.EventTracker()
        for index in range(1, 9):
            values = (53.0, 1.02, 0.03) if index <= 3 else (47.5, 1.06, 0.03)
            reading = detector.analyze(self.record(index, values))
            tracker.update(reading)
        event = tracker.active["equipment"]
        self.assertEqual(event["level"], 3)
        self.assertEqual(event["kind"], "pressure")
        self.assertEqual(event["sensors"], ["pressure"])
        self.assertEqual(event["current_level"], 1)
        self.assertEqual(event["sensor_history"], ["temp", "pressure"])
        self.assertEqual(event["evidence_row"], reading["row"])
        self.assertIn("temp", " ".join(event["peak_evidence"]))
        self.assertIn("壓力", tracker.work_orders[event["id"]]["problem"])

    def test_model_persistence_uses_only_complete_original_values(self):
        detector = monitor.Monitor(self.baseline)
        with mock.patch.object(detector, "score", return_value=100.0):
            readings = [detector.analyze(self.record(i)) for i in range(1, 4)]
            self.assertEqual([r["equipment_level"] for r in readings], [1, 1, 2])
            detector.analyze(self.record(4, (None, 1.02, 0.03)))
            self.assertEqual(detector.model_count, 0)

    def test_model_attention_threshold_is_shared_with_operation_observation(self):
        detector = monitor.Monitor(self.baseline)
        for offset, score, expected, observation in (
                (1, 96.0, 0, "與平常相近"),
                (2, 98.0, 1, "與平常不同")):
            with self.subTest(score=score):
                with mock.patch.object(detector, "score", return_value=score):
                    reading = detector.analyze(self.record(offset))
                self.assertEqual(reading["equipment_level"], expected)
                output = io.StringIO()
                console.Console(file=output, width=120).print(
                    main.dashboard(reading, monitor.EventTracker(), 120))
                self.assertIn(f"運轉觀察：{observation}", output.getvalue())

    def test_model_preserves_nine_minute_warning_without_quiet_normal_alerts(self):
        before = hashlib.sha256(CSV.read_bytes()).hexdigest()
        result = evaluate_model.evaluate()
        self.assertEqual(result["complete_rows"], 237)
        self.assertEqual(result["quiet_normal_rows"], 152)
        self.assertEqual(result["quiet_new_watch_rows"], [])
        self.assertEqual(result["watch_percentile"], 98)
        self.assertEqual(result["warning_percentile"], 98)
        self.assertEqual(result["first_strict_temp_row"], 310)
        self.assertEqual(result["first_drift_model_warning_row"], 301)
        self.assertEqual(result["lead_minutes"], 9)
        self.assertEqual(result["event_hits"], ["sudden", "temp_drift", "joint_gray"])
        self.assertEqual([result["physical_event_metrics"][key]
                          for key in ("tp", "fp", "fn", "tn")], [72, 1, 4, 160])
        self.assertNotIn("reference", result)
        self.assertNotIn("current", result)
        self.assertEqual(hashlib.sha256(CSV.read_bytes()).hexdigest(), before)

    def test_event_dedup_escalation_recovery_and_draft_status(self):
        detector = monitor.Monitor(self.baseline)
        tracker = monitor.EventTracker()
        with mock.patch.object(detector, "score", return_value=0.0):
            for index in range(1, 7):
                tracker.update(detector.analyze(self.record(index, (53, 1.02, 0.03))))
            event = tracker.active["equipment"]
            identifier = event["id"]
            self.assertEqual(event["level"], 3)
            self.assertEqual(len(tracker.work_orders), 1)
            for index in (7, 8):
                tracker.update(detector.analyze(self.record(index)))
                self.assertIn("equipment", tracker.active)
            tracker.update(detector.analyze(self.record(9)))
        self.assertNotIn("equipment", tracker.active)
        self.assertEqual(tracker.work_orders[identifier]["status"], "draft")
        self.assertEqual(tracker.work_orders[identifier]["event_status"], "resolved")

    def test_missing_data_cannot_clear_an_existing_warning(self):
        detector = monitor.Monitor(self.baseline)
        tracker = monitor.EventTracker()
        with mock.patch.object(detector, "score", return_value=0.0):
            tracker.update(detector.analyze(self.record(1, (53, 1.02, 0.03))))
            for index in range(2, 5):
                reading = detector.analyze(self.record(index, (None, 1.02, 0.03)))
                tracker.update(reading)
                self.assertEqual(reading["equipment_level"], 2)
                self.assertEqual(tracker.active["equipment"]["recover_count"], 0)

    def test_table_columns_stay_fixed_when_cell_text_changes(self):
        detector = monitor.Monitor(self.baseline)
        with mock.patch.object(detector, "score", return_value=0.0):
            readings = [detector.analyze(self.record(i)) for i in range(1, 7)]
            readings.append(detector.analyze(self.record(7, (None, 1.02, 0.03))))
            readings.append(detector.analyze(self.record(8, (47.5, 999, 0.03))))
        for width in (80, 120):
            positions = []
            for reading in readings:
                rendered = io.StringIO()
                screen = console.Console(file=rendered, width=width)
                screen.print(main.dashboard(reading, monitor.EventTracker(), width))
                heading = next(line for line in rendered.getvalue().splitlines()
                               if "感測器" in line and "原始回報" in line)
                positions.append(tuple(cells.cell_len(heading[:index])
                                       for index, char in enumerate(heading)
                                       if char == "│"))
            self.assertEqual(len(set(positions)), 1)
            self.assertEqual(positions[0][-1], width - 1)

    def test_guidance_prioritizes_actions_and_handles_recovery(self):
        detector = monitor.Monitor(self.baseline)
        tracker = monitor.EventTracker()

        def render():
            text = io.StringIO()
            console.Console(file=text, width=100).print(main.guidance(tracker))
            return text.getvalue()

        with mock.patch.object(detector, "score", return_value=0.0):
            reading = detector.analyze(self.record(1, (53, 1.02, 0.03)))
            tracker.update(reading)
            self.assertIn("建議處置", render())
            self.assertIn("優先檢查方向", render())
            reading = detector.analyze(self.record(2))
            tracker.update(reading)
            self.assertIn("正在確認恢復", render())
            self.assertIn("再確認 2 筆", render())
            reading = detector.analyze(self.record(3, (None, 1.02, 0.03)))
            tracker.update(reading)
            self.assertIn("先前提醒暫時保留", render())
            self.assertIn("先恢復有效回報", render())

    def test_equipment_advice_follows_affected_sensor_combination(self):
        cases = [
            ((47.5, 1.02, 0.08), ["vibration"], ["振動感測器", "軸承座"]),
            ((47.5, 1.09, 0.08), ["pressure", "vibration"], ["管路", "軸承座"]),
            ((47.5, 1.06, 0.06), ["pressure", "vibration"], ["管路", "聯軸器"]),
            ((53.0, 1.02, 0.08), ["temp", "vibration"], ["冷卻風扇", "潤滑"]),
            ((53.0, 1.09, 0.08), ["temp", "pressure", "vibration"], ["管路", "冷卻"]),
        ]
        for values, sensors, components in cases:
            with self.subTest(values=values):
                detector = monitor.Monitor(self.baseline)
                tracker = monitor.EventTracker()
                with mock.patch.object(detector, "score", return_value=0.0):
                    tracker.update(detector.analyze(self.record(1, values)))
                event = tracker.active["equipment"]
                self.assertEqual(event["sensors"], sensors)
                text = event["advice"]["recommendation"]
                self.assertEqual(text, "".join(event["advice"]["actions"]))
                for component in components:
                    self.assertIn(component, text)
                if event["level"] >= 2:
                    self.assertEqual(
                        tracker.work_orders[event["id"]]["recommendation"], text)

    def test_quality_advice_names_sensor_without_assuming_mechanical_fault(self):
        cases = [
            ((47.5, 999, 0.03), 1, "quality:invalid:pressure", "壓力"),
            ((None, 1.02, 0.03), 1, "quality:missing:temp", "溫度"),
        ]
        for values, count, key, name in cases:
            with self.subTest(key=key):
                detector = monitor.Monitor(self.baseline)
                tracker = monitor.EventTracker()
                with mock.patch.object(detector, "score", return_value=0.0):
                    for index in range(1, count + 1):
                        tracker.update(detector.analyze(self.record(index, values)))
                event = tracker.active[key]
                text = event["advice"]["recommendation"]
                self.assertIn(name, text)
                self.assertIn("感測器", text)
                self.assertIn("PLC", text)
                self.assertNotIn("軸承", text)


class TerminalTest(unittest.TestCase):

    def test_sensor_region_respects_boundaries_and_invalid_reports(self):
        cases = [
            ("temp", 45.0, True, "正常範圍"),
            ("temp", 50.0, True, "正常範圍"),
            ("temp", 43.0, True, "灰區偏低"),
            ("temp", 52.0, True, "灰區偏高"),
            ("temp", 42.9, True, "低於下限"),
            ("temp", 52.1, True, "超過上限"),
            ("pressure", 0.97, True, "灰區偏低"),
            ("pressure", 1.08, True, "灰區偏高"),
            ("pressure", 0.96, True, "低於下限"),
            ("pressure", 1.09, True, "超過上限"),
            ("vibration", 0.02, True, "正常範圍"),
            ("vibration", 0.04, True, "正常範圍"),
            ("vibration", 0.01, True, "灰區偏低"),
            ("vibration", 0.07, True, "灰區偏高"),
            ("vibration", 0.08, True, "超過上限"),
            ("pressure", 999.0, False, "無效數值"),
            ("temp", None, False, "未回報"),
        ]
        for sensor, value, valid, expected in cases:
            with self.subTest(sensor=sensor, value=value):
                self.assertEqual(main.value_region(sensor, value, valid)[0], expected)

    def test_interruption_saves_partial_results(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = pathlib.Path(directory)
            with mock.patch("main.time.sleep", side_effect=KeyboardInterrupt):
                summary = main.run(CSV, output_dir, 0.01,
                                   console.Console(file=io.StringIO()))
            self.assertEqual(summary["status"], "interrupted")
            self.assertEqual(summary["monitored_rows"], 1)
            self.assertTrue((output_dir / "events.json").is_file())
            self.assertTrue((output_dir / "work_orders.json").is_file())
            report = (output_dir / "results.html").read_text(encoding="utf-8")
            self.assertIn("已停止播放", report)
            self.assertIn("僅保留已讀取的結果", report)

    def test_live_chart_window_contains_only_already_read_records(self):
        with tempfile.TemporaryDirectory() as directory:
            screen = console.Console(file=io.StringIO(), force_terminal=True,
                                     width=80, height=24)
            with mock.patch("main.time.sleep", side_effect=KeyboardInterrupt), (
                    mock.patch("main.dashboard", wraps=main.dashboard)) as draw:
                summary = main.run(CSV, pathlib.Path(directory), 0.01, screen)
            self.assertEqual(summary["monitored_rows"], 1)
            draw.assert_called_once()
            reading, _, width, history, height = draw.call_args.args
            self.assertEqual((width, height), (80, 24))
            self.assertEqual(len(history), 60)
            self.assertEqual(history[0]["row"], 192)
            self.assertEqual(history[-1]["row"], 251)
            self.assertEqual(history[-1]["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
                             reading["timestamp"])

    def test_truncated_csv_is_not_reported_as_finished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "short.csv"
            source.write_text("\n".join(CSV.read_text().splitlines()[:261]) + "\n")
            with self.assertRaises(ValueError):
                main.run(source, root / "out", 0,
                         console.Console(file=io.StringIO()))
            summary = json.loads((root / "out" / "summary.json").read_text())
            self.assertEqual(summary["status"], "incomplete")
            self.assertEqual(summary["monitored_rows"], 10)
            self.assertIn("資料未完整讀取",
                          (root / "out/results.html").read_text(encoding="utf-8"))

    def test_invalid_playback_interval_is_rejected(self):
        for interval in ("-1", "nan", "inf"):
            with self.subTest(interval=interval):
                with mock.patch("sys.argv", ["main.py", "--interval", interval]):
                    with contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as caught:
                            main.main()
                self.assertEqual(caught.exception.code, 2)

    def test_full_stream_preserves_csv_and_does_not_open_metadata(self):
        before = hashlib.sha256(CSV.read_bytes()).hexdigest()
        original_open = pathlib.Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name == "generation_metadata.json":
                raise AssertionError("監測不可讀取生成答案")
            return original_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            output_dir = pathlib.Path(directory)
            text = io.StringIO()
            with mock.patch.object(pathlib.Path, "open", guarded_open):
                summary = main.run(CSV, output_dir, 0,
                                   console.Console(file=text, width=100))
            self.assertEqual(summary["monitored_rows"], 250)
            self.assertEqual(summary["status"], "finished")
            self.assertEqual(sum(summary["missing_by_sensor"].values()), 12)
            self.assertEqual(sum(summary["imputed_by_sensor"].values()), 11)
            readings = [json.loads(line) for line in
                        (output_dir / "readings.jsonl").read_text().splitlines()]
            spike = next(item for item in readings if item["row"] == 270)
            self.assertEqual(spike["equipment_level"], 3)
            gray = next(item for item in readings if item["row"] == 402)
            self.assertGreaterEqual(gray["equipment_level"], 2)
            self.assertIsNone(next(item for item in readings
                                   if item["row"] == 452)["effective"]["temp"])
            orders = json.loads((output_dir / "work_orders.json").read_text())
            self.assertEqual(len({order["id"] for order in orders}), len(orders))
            events = json.loads((output_dir / "events.json").read_text())
            self.assertFalse(any(event["kind"] == "stable" for event in events))
            repeated = next(item for item in readings if item["row"] == 372)
            self.assertEqual(repeated["unchanged_samples"]["vibration"], 8)
            self.assertEqual(repeated["quality_level"], 0)
            rendered = io.StringIO()
            console.Console(file=rendered, width=100).print(
                main.dashboard(repeated, monitor.EventTracker(), 100))
            self.assertIn("有效回報", rendered.getvalue())
            self.assertNotIn("卡值", rendered.getvalue())
            self.assertTrue(all(order["status"] == "draft" for order in orders))
            report = (output_dir / "results.html").read_text(encoding="utf-8")
            self.assertIn("設備監測結果", report)
            self.assertIn("工單：草稿待確認", report)
            self.assertIn("優先檢查", report)
            self.assertIn("2026-11-07 16:10:00", report)
            self.assertIn("2026-11-07 20:19:00", report)
            self.assertIn("監測完成", report)
            for order in orders:
                self.assertIn(order["id"], report)
                self.assertIn(order["actions"][0], report)
            for internal in ("score_samples", "score_provisional", "max_samples"):
                self.assertNotIn(internal, report)
            metadata = json.loads(
                (ROOT / "data" / "generation_metadata.json").read_text())
            excluded = {row for event in metadata["events"] for row in range(
                event["start_row"], event["end_row"] + 1)}
            excluded.update(cell["row"] for cell in metadata["random_missing_cells"])
            excluded.update(
                metadata["validation"]["normal_sensor_abnormal_label_rows"])
            normal = [r for r in readings if r["row"] not in excluded
                      and all(r["raw_valid"].values())]
            held_warnings = sum((r["equipment_level"] or 0) >= 2 for r in normal)
            self.assertLessEqual(held_warnings / len(normal), 0.05)
            self.assertTrue(any((r["equipment_level"] or 0) >= 1
                                for r in readings if 295 <= r["row"] < 310))
            for width in (80, 120):
                rendered = io.StringIO()
                screen = console.Console(file=rendered, width=width)
                tracker = monitor.EventTracker()
                tracker.update(spike)
                screen.print(main.dashboard(spike, tracker, width))
                self.assertIn("危急", rendered.getvalue())
                self.assertIn("設備綜合評估", rendered.getvalue())
                self.assertIn("AOI設備監測", rendered.getvalue())
                self.assertNotIn("模擬回報", rendered.getvalue())
                self.assertIn("優先檢查方向", rendered.getvalue())
                self.assertIn("草稿，待確認", rendered.getvalue())
                self.assertIn("運轉觀察", rendered.getvalue())
                self.assertIn("作業條件", rendered.getvalue())
                self.assertIn("超過上限", rendered.getvalue())
                self.assertNotIn("百分位", rendered.getvalue())
                self.assertNotIn("/500", rendered.getvalue())
            self.assertIn("監測完成", text.getvalue())
        self.assertEqual(hashlib.sha256(CSV.read_bytes()).hexdigest(), before)

    def test_reader_rejects_duplicate_time_and_bad_labels(self):
        header = "timestamp,temp,pressure,vibration,label\n"
        examples = [
            "2026-11-07 12:00:00,47.5,1.02,0.03,normal\n"
            "2026-11-07 12:00:00,47.5,1.02,0.03,normal\n",
            "2026-11-07 12:00:00,47.5,1.02,0.03,unknown\n",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "bad.csv"
            for content in examples:
                path.write_text(header + content)
                with self.subTest(content=content), self.assertRaises(ValueError):
                    list(monitor.read_records(path))


if __name__ == "__main__":
    unittest.main()
