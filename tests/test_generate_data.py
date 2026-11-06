"""檢查生成資料的規格、門檻邊界與檔案可重現性。"""

import csv
import pathlib
import tempfile
import unittest

import numpy as np
import pandas as pd

import generate_data


class GenerateDataTest(unittest.TestCase):

    def test_label_probability_boundaries(self):
        values = np.array([
            [47.5, 1.025, 0.03],
            [45.0, 1.00, 0.02],
            [50.0, 1.05, 0.04],
            [51.0, 1.025, 0.03],
            [51.0, 1.06, 0.03],
            [51.0, 1.06, 0.06],
            [43.0, 0.97, 0.07],
            [52.0, 1.08, 0.07],
            [np.nextafter(43.0, -np.inf), 1.025, 0.03],
            [np.nextafter(52.0, np.inf), 1.025, 0.03],
            [47.5, np.nextafter(0.97, -np.inf), 0.03],
            [47.5, np.nextafter(1.08, np.inf), 0.03],
            [47.5, 1.025, np.nextafter(0.07, np.inf)],
            [47.5, 1.025, 0.01],
        ])
        expected = [0.01, 0.01, 0.01, 0.1, 0.4, 0.8, 0.8, 0.8,
                    1, 1, 1, 1, 1, 0.1]
        np.testing.assert_array_equal(
            generate_data.label_probabilities(values), expected)

    def test_baseline_and_quality_faults(self):
        frame, metadata = generate_data.generate_data()
        result = generate_data.validate_data(frame)
        self.assertTrue(all(result["checks"].values()))
        self.assertEqual(metadata["seed"], 42)
        self.assertEqual(frame.loc[384, "pressure"], 999)
        self.assertEqual(frame.loc[384, "label"], "normal")
        self.assertTrue(frame.loc[449:452, "temp"].isna().all())
        for autocorrelation in result["baseline_lag1"].values():
            self.assertGreater(autocorrelation, 0.4)
        correlation = frame.loc[:249, generate_data.SENSORS].corr()
        for first, second in [("temp", "pressure"), ("temp", "vibration"),
                              ("pressure", "vibration")]:
            self.assertGreater(correlation.loc[first, second], 0.1)

    def test_invalid_data_is_rejected(self):
        frame, _ = generate_data.generate_data()
        cases = []
        no_sudden = frame.copy()
        no_sudden.loc[269, generate_data.SENSORS] = [47.5, 1.025, 0.03]
        cases.append(no_sudden)
        wrong_time = frame.copy()
        wrong_time.loc[1, "timestamp"] = wrong_time.loc[0, "timestamp"]
        cases.append(wrong_time)
        missing_label = frame.copy()
        missing_label.loc[0, "label"] = None
        cases.append(missing_label)
        excessive_precision = frame.copy()
        excessive_precision.loc[0, "pressure"] = 1.025
        cases.append(excessive_precision)
        for invalid in cases:
            with self.subTest(), self.assertRaises(ValueError):
                generate_data.validate_data(invalid)

    def test_csv_precision_missing_values_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = pathlib.Path(directory)
            generate_data.save_data(output_dir)
            csv_path = output_dir / "sensor_data.csv"
            json_path = output_dir / "generation_metadata.json"
            original_csv = csv_path.read_bytes()
            original_json = json_path.read_bytes()
            restored = pd.read_csv(csv_path, parse_dates=["timestamp"],
                                   float_precision="round_trip")
            expected, _ = generate_data.generate_data()
            pd.testing.assert_frame_equal(restored, expected, check_exact=True)
            with csv_path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 500)
            for row in rows:
                for sensor, decimals in generate_data.DECIMALS.items():
                    if row[sensor]:
                        self.assertRegex(
                            row[sensor], rf"^-?\d+\.\d{{{decimals}}}$")
            self.assertEqual(rows[269]["temp"], "55.0")
            self.assertEqual(rows[269]["vibration"], "0.10")
            self.assertEqual(rows[384]["pressure"], "999.00")
            self.assertEqual(rows[449]["temp"], "")
            self.assertEqual(
                len({row["vibration"] for row in rows[364:372]}), 1)
            generate_data.save_data(output_dir)
            self.assertEqual(csv_path.read_bytes(), original_csv)
            self.assertEqual(json_path.read_bytes(), original_json)


if __name__ == "__main__":
    unittest.main()
