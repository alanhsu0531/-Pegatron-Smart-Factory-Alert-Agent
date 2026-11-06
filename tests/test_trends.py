"""驗證趨勢邊界、缺值斷點、原始資料窗口與終端排版。"""

import collections
import datetime
import io
import pathlib
import unittest

from rich import cells
from rich import console

import main
import monitor
import trends


CSV = pathlib.Path(__file__).resolve().parents[1] / "data" / "sensor_data.csv"


class TrendsTest(unittest.TestCase):

    def records(self, count=60):
        """建立逐分鐘的正常原始量測。"""
        start = datetime.datetime(2026, 11, 7, 16)
        return [{"timestamp": start + datetime.timedelta(minutes=i),
                 "temp": 47.5, "pressure": 1.02, "vibration": 0.03}
                for i in range(count)]

    def test_boundaries_are_visible_and_equal_values_are_not_exceedances(self):
        cases = [("temp", 43.0, 42.9), ("temp", 52.0, 52.1),
                 ("pressure", 0.97, 0.96), ("pressure", 1.08, 1.09),
                 ("vibration", 0.07, 0.08)]
        for sensor, boundary, exceeded in cases:
            with self.subTest(sensor=sensor, boundary=boundary):
                records = self.records(2)
                records[-1][sensor] = boundary
                rendered = trends.sensor_chart(records, sensor, 40).plain
                self.assertIn(f"{boundary:.{monitor.DECIMALS[sensor]}f}", rendered)
                self.assertIn("┈", rendered)
                self.assertNotIn("●", rendered)
                records[-1][sensor] = exceeded
                self.assertIn("●", trends.sensor_chart(records, sensor, 40).plain)
        vibration = trends.sensor_chart(self.records(2), "vibration", 40).plain
        self.assertEqual(sum("┈" in line for line in vibration.splitlines()), 1)

    def test_missing_and_invalid_values_leave_real_gaps_without_stretching_axis(self):
        records = self.records(6)
        for row, value in zip(records, [47.5, None, 999, float("nan"),
                                       float("inf"), 47.5]):
            row["temp"] = value
        broken = trends.sensor_chart(records, "temp", 42, 9).plain
        self.assertNotIn("999", broken)
        self.assertNotIn("nan", broken)
        self.assertNotIn("inf", broken)

        def has_middle_point(rendered):
            return any("\u2800" < line[21] <= "\u28ff"
                       for line in rendered.splitlines())

        self.assertFalse(has_middle_point(broken))
        for row in records:
            row["temp"] = 47.5
        self.assertTrue(has_middle_point(
            trends.sensor_chart(records, "temp", 42, 9).plain))
        for row in records:
            row["temp"] = None
        unavailable = trends.sensor_chart(records, "temp", 42, 9).plain
        self.assertIn("43.0", unavailable)
        self.assertIn("52.0", unavailable)
        self.assertFalse(any("\u2800" < char <= "\u28ff" for char in unavailable))

    def test_charts_inherit_terminal_background_without_white_reference_bands(self):
        records = self.records()
        records[-1].update(temp=55.0, pressure=1.12, vibration=0.10)
        for sensor in monitor.SENSORS:
            with self.subTest(sensor=sensor):
                chart = trends.sensor_chart(records, sensor, 40)
                self.assertIn("┈", chart.plain)
                self.assertIn("●", chart.plain)
                for span in chart.spans:
                    background = span.style.bgcolor
                    self.assertTrue(background is None or background.is_default)

    def test_charts_share_latest_hour_and_fit_terminal_width(self):
        records = self.records(70)
        records[0]["temp"] = 100.0
        for width in (80, 120):
            with self.subTest(width=width):
                output = io.StringIO()
                console.Console(file=output, width=width).print(
                    trends.trend_charts(records, width))
                rendered = output.getvalue()
                for label in ("溫度", "壓力", "振動", "43.0", "52.0",
                              "0.97", "1.08", "0.07", "16:10", "17:09"):
                    self.assertIn(label, rendered)
                self.assertNotIn("16:00", rendered)
                self.assertNotIn("100.0", rendered)
                self.assertTrue(all(cells.cell_len(line) <= width
                                    for line in rendered.splitlines()))

    def test_dashboard_keeps_charts_and_priority_action_visible_in_small_window(self):
        records = list(monitor.read_records(CSV))
        detector = monitor.Monitor(records[:250])
        tracker = monitor.EventTracker()
        history = collections.deque(records[190:250], maxlen=60)
        for record in records[250:]:
            reading = detector.analyze(record)
            tracker.update(reading)
            history.append(record)
            if record["row"] not in (251, 270, 310, 345, 385, 402, 452, 500):
                continue
            for width, height in ((80, 24), (120, 40)):
                with self.subTest(row=record["row"], width=width, height=height):
                    output = io.StringIO()
                    console.Console(file=output, width=width).print(
                        main.dashboard(reading, tracker, width, history, height))
                    rendered = output.getvalue()
                    self.assertLessEqual(len(rendered.splitlines()), height)
                    for label in ("溫度", "壓力", "振動", "紅虛線", "原始回報",
                                  "設備綜合評估", "AOI設備監測", "工單草稿"):
                        self.assertIn(label, rendered)
                    if tracker.active:
                        self.assertIn("目前狀況", rendered)
                    if record["row"] == 270:
                        self.assertIn("通知現場負責人", rendered)


if __name__ == "__main__":
    unittest.main()
