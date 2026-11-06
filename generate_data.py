"""產生固定主種子的設備資料與驗收註記。

執行 .venv/bin/python generate_data.py，預設輸出至專案 data 目錄。
本模組僅生成資料，不執行預警、補值或模型訓練。
"""

import argparse
import json
import pathlib
import platform

import numpy as np
import pandas as pd
from rich import console
from rich import table
from scipy import stats


SEED = 42
ROWS = 500
SENSORS = ["temp", "pressure", "vibration"]
DECIMALS = {"temp": 1, "pressure": 2, "vibration": 2}
NORMAL_LOW = np.array([45.0, 1.00, 0.02])
NORMAL_HIGH = np.array([50.0, 1.05, 0.04])
STRICT_LOW = np.array([43.0, 0.97, -np.inf])
STRICT_HIGH = np.array([52.0, 1.08, 0.07])
RHO = 0.8
GAMMA_SHAPE = 36.0
EVENTS = [
    ("sudden", "三感測器突發", 270, 270, SENSORS),
    ("temp_drift", "溫度漸升、超標與恢復", 295, 349, ["temp"]),
    ("stuck", "振動卡值", 365, 372, ["vibration"]),
    ("sentinel", "壓力回傳 999", 385, 385, ["pressure"]),
    ("joint_gray", "灰區聯合異常", 400, 419, SENSORS),
    ("missing", "溫度連續缺值", 450, 453, ["temp"]),
]


def generate_baseline(rng: np.random.Generator) -> np.ndarray:
    """產生常態溫壓與 Gamma 振動，保留時間及欄位相關性。

    Args:
        rng: 基線專用的亂數產生器。

    Returns:
        500 列、依 SENSORS 排序的量測陣列。
    """
    covariance = [[1.0, 0.55, 0.75], [0.55, 1.0, 0.65],
                  [0.75, 0.65, 1.0]]
    noise = rng.multivariate_normal(np.zeros(3), covariance, ROWS + 2)
    latent = noise.copy()
    for index in range(1, len(latent)):
        latent[index] = (RHO * latent[index - 1]
                         + np.sqrt(1 - RHO**2) * noise[index])

    # Gaussian copula 保留 Gamma 邊際；溫度落後共同波動兩分鐘。
    quantiles = np.clip(stats.norm.cdf(latent[2:, 2]), 1e-12, 1 - 1e-12)
    return np.column_stack([
        47.5 + 0.7 * latent[:-2, 0],
        1.025 + 0.007 * latent[2:, 1],
        stats.gamma.ppf(quantiles, a=GAMMA_SHAPE, scale=0.03 / GAMMA_SHAPE),
    ])


def label_probabilities(values: np.ndarray) -> np.ndarray:
    """依有效、尚未受通訊故障污染的量測計算標籤機率。

    Args:
        values: 每列依 temp、pressure、vibration 排序的有效量測。

    Returns:
        每列 label 為 abnormal 的機率；等於嚴格邊界不算超標。
    """
    strict = ((values < STRICT_LOW) | (values > STRICT_HIGH)).any(axis=1)
    gray_count = ((values < NORMAL_LOW) | (values > NORMAL_HIGH)).sum(axis=1)
    return np.where(strict, 1.0, np.array([0.01, 0.10, 0.40, 0.80])[gray_count])


def generate_data() -> tuple[pd.DataFrame, dict]:
    """以主種子生成資料，先產生設備標籤，再加入回傳故障。

    Returns:
        原始資料與獨立驗收註記；註記不可提供給未來的偵測器或 Agent。
    """
    baseline_rng, event_rng, label_rng, missing_rng = [
        np.random.default_rng(seed)
        for seed in np.random.SeedSequence(SEED).spawn(4)
    ]
    physical = generate_baseline(baseline_rng)
    physical[269] = [55.0, 1.12, 0.10]
    physical[294:324, 0] = np.linspace(physical[293, 0], 55.0, 30)
    physical[324:344, 0] = 55.0 + event_rng.uniform(-0.15, 0.15, 20)
    physical[344:349, 0] = np.linspace(physical[343, 0], 47.5, 5)
    physical[399:419] = [51.0, 1.065, 0.055] + event_rng.normal(
        size=(20, 3)) * [0.12, 0.002, 0.002]
    # 先按回報解析度量化，label 與之後可見的數值使用相同門檻。
    for index, sensor in enumerate(SENSORS):
        physical[:, index] = np.round(physical[:, index], DECIMALS[sensor])
    labels = np.where(
        label_rng.random(ROWS) < label_probabilities(physical),
        "abnormal", "normal")

    observed = physical.copy()
    # 卡值使用可回報的正常值，並確保事件起點與前一筆不同。
    observed[364:372, 2] = 0.04 if observed[363, 2] != 0.04 else 0.02
    observed[384, 1] = 999.0
    observed[449:453, 0] = np.nan

    eligible = np.ones((ROWS, 3), dtype=bool)
    eligible[:250] = False
    for _, _, start, end, _ in EVENTS:
        eligible[start - 1:end] = False
    normal = ((physical >= NORMAL_LOW) & (physical <= NORMAL_HIGH)).all(axis=1)
    eligible[normal & (labels == "abnormal")] = False
    missing_cells = missing_rng.choice(
        np.flatnonzero(eligible), 8, replace=False)
    observed.flat[missing_cells] = np.nan

    frame = pd.DataFrame(observed, columns=SENSORS)
    frame.insert(0, "timestamp", pd.date_range("2026-11-07 12:00:00",
                                            periods=ROWS, freq="min"))
    frame["label"] = labels
    metadata = {
        "purpose": "僅供資料生成驗收，不得輸入偵測器或 Agent",
        "seed": SEED,
        "python": platform.python_version(),
        "row_numbering": "從 1 開始，不含 CSV 標頭",
        "measurement_decimals": DECIMALS,
        "distribution_scope": "常態與 Gamma 描述量化前基線，回報值再四捨五入",
        "baseline_parameters": {
            "temp_mean": 47.5, "temp_std": 0.7,
            "pressure_mean": 1.025, "pressure_std": 0.007,
            "vibration_gamma_shape": GAMMA_SHAPE,
            "vibration_gamma_scale": 0.03 / GAMMA_SHAPE,
            "latent_ar1": RHO, "temp_lag_minutes": 2,
        },
        "label_policy": {
            "source": "先依設備量測生成，之後才加入回傳故障與缺值",
            "strict_probability": 1.0,
            "probabilities_by_gray_count": [0.01, 0.10, 0.40, 0.80],
        },
        "events": [
            {"type": kind, "name": name, "start_row": start,
             "end_row": end, "sensors": sensors,
             "start_time": str(frame.at[start - 1, "timestamp"]),
             "end_time": str(frame.at[end - 1, "timestamp"])}
            for kind, name, start, end, sensors in EVENTS
        ],
        "random_missing_cells": [
            {"row": int(cell // 3 + 1), "sensor": SENSORS[cell % 3]}
            for cell in sorted(missing_cells)
        ],
    }
    return frame, metadata


def validate_data(frame: pd.DataFrame) -> dict:
    """檢查交付資料的規格與指定情境，不進行異常偵測效能評估。

    Args:
        frame: 包含五個指定欄位的原始資料。

    Returns:
        驗收結果與前 250 筆的簡短統計。

    Raises:
        ValueError: 資料規格、標籤比例或指定情境不符合要求。
    """
    if (list(frame.columns) != ["timestamp", *SENSORS, "label"]
            or len(frame) != ROWS):
        raise ValueError("資料必須為 500 筆且包含五個指定欄位。")
    values = frame[SENSORS].to_numpy()
    valid = np.isfinite(values) & (values != 999)
    strict = valid & ((values < STRICT_LOW) | (values > STRICT_HIGH))
    normal = (valid & (values >= NORMAL_LOW)
              & (values <= NORMAL_HIGH)).all(axis=1)
    disagreement = normal & frame["label"].eq("abnormal").to_numpy()
    abnormal_count = int(frame["label"].eq("abnormal").sum())
    gray = values[399:419]
    checks = {
        "量測小數位符合範例": frame[SENSORS].equals(
            frame[SENSORS].round(DECIMALS)),
        "每分鐘一筆且時間唯一": bool(
            frame["timestamp"].diff().iloc[1:].eq(pd.Timedelta(minutes=1)).all()
            and frame["timestamp"].is_unique),
        "標籤合法且異常率 10–15%": bool(
            frame["label"].isin(["normal", "abnormal"]).all()
            and 50 <= abnormal_count <= 75),
        "前 250 筆穩態且無缺值": bool(
            valid[:250].all() and not strict[:250].any()),
        "振動恆正且沒有無限值": bool(
            (values[:, 2][~np.isnan(values[:, 2])] > 0).all()
            and not np.isinf(values).any()),
        "有效嚴格超標皆為 abnormal": bool(
            frame.loc[strict.any(axis=1), "label"].eq("abnormal").all()),
        "突發三感測器超標": bool(strict[269].all()),
        "溫度逐步升高並維持超標": bool(
            (np.diff(values[294:324, 0]) > 0).all()
            and (values[324:344, 0] > 52).all()
            and 45 <= values[348, 0] <= 50),
        "卡值保持回報精度": bool(
            len(np.unique(values[364:372, 2])) == 1
            and values[363, 2] != values[364, 2]),
        "壓力錯誤碼獨立存在": bool(
            values[384, 1] == 999 and (values == 999).sum() == 1),
        "灰區三欄偏高但未嚴格超標": bool(
            (gray > NORMAL_HIGH).all() and (gray <= STRICT_HIGH).all()),
        "連續與隨機缺值均保留": bool(
            np.isnan(values[449:453, 0]).all()
            and np.isnan(values).sum() == 12),
        "監測區段有正常量測的 abnormal 標籤": bool(disagreement[250:].any()),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("資料檢查未通過：" + "、".join(failed))

    baseline = frame.loc[:249, SENSORS]
    return {
        "checks": checks,
        "rows": len(frame),
        "abnormal_count": abnormal_count,
        "abnormal_rate": abnormal_count / len(frame),
        "missing_by_sensor": frame[SENSORS].isna().sum().astype(int).to_dict(),
        "normal_sensor_abnormal_label_rows": (
            np.flatnonzero(disagreement) + 1).tolist(),
        "baseline_statistics": baseline.agg(
            ["mean", "std", "min", "max"]).to_dict(),
        "baseline_lag1": {
            name: float(baseline[name].autocorr()) for name in SENSORS},
        "baseline_correlation": baseline.corr().to_dict(),
    }


def save_data(output_dir: pathlib.Path) -> dict:
    """檢查資料後輸出原始 CSV 與獨立的 JSON 驗收註記。

    Args:
        output_dir: 生成檔案的儲存目錄。

    Returns:
        包含事件及檢查結果的註記。

    Raises:
        ValueError: 生成的資料未通過檢查。
        OSError: 無法建立目錄或寫入檔案。
    """
    frame, metadata = generate_data()
    metadata["validation"] = validate_data(frame)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_frame = frame.copy()
    for sensor, decimals in DECIMALS.items():
        csv_frame[sensor] = frame[sensor].map(
            lambda value: f"{value:.{decimals}f}", na_action="ignore")
    csv_frame.to_csv(output_dir / "sensor_data.csv", index=False,
                     date_format="%Y-%m-%d %H:%M:%S")
    text = json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False)
    (output_dir / "generation_metadata.json").write_text(
        text + "\n", encoding="utf-8")
    return metadata


def main() -> None:
    """生成資料並顯示使用者驗收所需的簡短摘要。"""
    parser = argparse.ArgumentParser(description="產生單一主種子的設備模擬資料")
    parser.add_argument(
        "--output-dir", type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent / "data")
    args = parser.parse_args()
    output = console.Console()
    try:
        metadata = save_data(args.output_dir)
    except (OSError, ValueError) as error:
        output.print(f"[red]資料生成失敗：[/red]{error}", markup=True)
        raise SystemExit(1) from error
    result = metadata["validation"]
    output.print("[bold green]資料生成完成，等待你確認[/bold green]")
    output.print(f"500 筆｜每分鐘一筆｜主種子 {SEED}｜"
                 f"abnormal {result['abnormal_count']} 筆"
                 f"（{result['abnormal_rate']:.1%}）")
    missing = result["missing_by_sensor"]
    output.print(f"缺值：temp {missing['temp']}、pressure {missing['pressure']}、"
                 f"vibration {missing['vibration']}；原始資料未補值。")
    events = table.Table("已包含的情境", "資料列", header_style="bold cyan")
    for event in metadata["events"]:
        events.add_row(
            event["name"], f"{event['start_row']}–{event['end_row']}")
    events.add_row("隨機缺值", "8 個儲存格")
    rows = result["normal_sensor_abnormal_label_rows"]
    events.add_row("正常量測但 label abnormal", "、".join(map(str, rows)))
    output.print(events)
    output.print(f"[green]{len(result['checks'])} 項資料檢查通過。[/green]")
    output.print(f"CSV：{args.output_dir / 'sensor_data.csv'}", markup=False)
    output.print("本階段到此為止，待資料確認後再進行後續功能。")


if __name__ == "__main__":
    main()
