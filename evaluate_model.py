"""離線評估目前模型提醒；生成註記僅在預測完成後用於驗收。"""

import hashlib
import json
import pathlib

import numpy as np
from rich import box
from rich import console
from rich import table
from sklearn import metrics

from factory_agent import monitor
from factory_agent import theme


ROOT = pathlib.Path(__file__).resolve().parent


def classification_metrics(truth: list, predicted: list, scores: list) -> dict:
    """計算逐筆提醒的命中、漏報與原始分數排序表現。

    Args:
        truth: 離線參考答案，不作為模型輸入。
        predicted: 模型單筆提醒，不含規則、AOI 或恢復等待。
        scores: 越高越異常的原始模型分數。

    Returns:
        混淆矩陣、Precision、Recall、F1、AP 與樣本數。
    """
    tn, fp, fn, tp = metrics.confusion_matrix(
        truth, predicted, labels=[False, True]).ravel()
    return {
        "rows": len(truth), "tp": int(tp), "fp": int(fp),
        "fn": int(fn), "tn": int(tn),
        "precision": float(metrics.precision_score(truth, predicted, zero_division=0)),
        "recall": float(metrics.recall_score(truth, predicted, zero_division=0)),
        "f1": float(metrics.f1_score(truth, predicted, zero_division=0)),
        "average_precision": float(metrics.average_precision_score(truth, scores)),
    }


def evaluate() -> dict:
    """逐筆重播主種子，完成預測後才載入事件答案。

    Returns:
        目前模型的逐筆指標、正常情境新提醒與漂移警告時間。

    Raises:
        ValueError: 主種子的筆數不是 500。
        OSError: 無法讀取驗收資料。
    """
    source = ROOT / "data/sensor_data.csv"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    records = monitor.read_records(source)
    rows = []
    try:
        baseline = monitor.initial_records(records)
        detector = monitor.Monitor(baseline)
        for record in records:
            reading = detector.analyze(record)
            complete = all(reading["raw_valid"].values())
            raw_score = None
            if complete:
                features = np.array([[record[s] for s in monitor.SENSORS]])
                raw_score = -float(detector.model.score_samples(features)[0])
            rows.append({
                "row": record["row"], "label": record["label"],
                "complete": complete, "gray": any(
                    record[s] < monitor.NORMAL_LOW[i] or record[s] > monitor.NORMAL_HIGH[i]
                    for i, s in enumerate(monitor.SENSORS)) if complete else None,
                "strict_temp": "temp" in reading["strict_sensors"],
                "score": reading["score"], "raw_score": raw_score,
                "model_warning": detector.model_count >= 3,
            })
    finally:
        records.close()
    if len(rows) != 250:
        raise ValueError("主種子驗收資料必須共 500 筆。")

    metadata = json.loads((ROOT / "data/generation_metadata.json").read_text(encoding="utf-8"))
    equipment_events = [e for e in metadata["events"]
                        if e["type"] in ("sudden", "temp_drift", "joint_gray")]
    physical = {i for e in equipment_events
                for i in range(e["start_row"], e["end_row"] + 1)}
    injected = {i for e in metadata["events"]
                for i in range(e["start_row"], e["end_row"] + 1)}
    injected.update(cell["row"] for cell in metadata["random_missing_cells"])
    eligible = [r for r in rows if r["complete"]]
    quiet = [r for r in eligible if r["row"] not in injected
             and r["label"] == "normal" and not r["gray"]]
    drift = next(e for e in equipment_events if e["type"] == "temp_drift")
    drift_rows = [r for r in rows if drift["start_row"] <= r["row"] <= drift["end_row"]]
    strict_first = next(r["row"] for r in drift_rows if r["strict_temp"])
    threshold = monitor.MODEL_WATCH_PERCENTILE
    predicted = [r["score"] >= threshold for r in eligible]
    raw_scores = [r["raw_score"] for r in eligible]
    first_warning = next((r["row"] for r in drift_rows if r["model_warning"]), None)
    return {
        "seed": metadata["seed"], "source_sha256": digest,
        "training_rows": detector.baseline_counts["training"],
        "calibration_rows": detector.baseline_counts["calibration"],
        "monitored_rows": len(rows), "complete_rows": len(eligible),
        "quality_rows_excluded_from_model_metrics": len(rows) - len(eligible),
        "quiet_normal_rows": len(quiet), "first_strict_temp_row": strict_first,
        "limitation": "主種子已參與開發，事件窗口為情境參考；尚非跨種子泛化結果。",
        "watch_percentile": threshold,
        "warning_percentile": monitor.MODEL_WARNING_PERCENTILE,
        "trees": detector.model.n_estimators,
        "physical_event_metrics": classification_metrics(
            [r["row"] in physical for r in eligible], predicted, raw_scores),
        "aoi_label_metrics": classification_metrics(
            [r["label"] == "abnormal" for r in eligible], predicted, raw_scores),
        "quiet_new_watch_rows": [r["row"] for r in quiet if r["score"] >= threshold],
        "first_drift_model_warning_row": first_warning,
        "lead_minutes": strict_first - first_warning if first_warning is not None else None,
        "event_hits": [e["type"] for e in equipment_events if any(
            e["start_row"] <= r["row"] <= e["end_row"] and r["score"] >= threshold
            for r in eligible)],
    }


def main() -> None:
    """儲存目前模型的評估結果並呈現精簡驗收表格。"""
    result = evaluate()
    destination = ROOT / "outputs/model_evaluation.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    summary = table.Table("驗收項目", "結果", box=box.SQUARE,
                          border_style=theme.PALETTE["border"],
                          header_style=f"bold {theme.PALETTE['muted']}")
    for key, label in (("precision", "提醒命中率"), ("recall", "事件窗口檢出率"), ("f1", "F1")):
        summary.add_row(label, f"{result['physical_event_metrics'][key]:.1%}")
    summary.add_row("原始分數 AP", f"{result['physical_event_metrics']['average_precision']:.3f}")
    summary.add_row("正常情境新模型關注", str(len(result["quiet_new_watch_rows"])))
    summary.add_row("溫度模型警告資料列", str(result["first_drift_model_warning_row"]))
    summary.add_row("比嚴格超標提前分鐘", str(result["lead_minutes"]))
    output = console.Console()
    output.print(summary)
    output.print(f"有效原始量測 {result['complete_rows']} 筆；正常情境 {result['quiet_normal_rows']} 筆。")
    output.print(result["limitation"])
    output.print("詳細結果：outputs/model_evaluation.json", style=theme.PALETTE["muted"])


if __name__ == "__main__":
    main()
