"""逐筆監測設備資料；只以起始基準訓練，不讀取生成事件註記。"""

import collections
import csv
import datetime
import itertools
import pathlib

import numpy as np
from sklearn import ensemble

import advice


SENSORS = ["temp", "pressure", "vibration"]
DECIMALS = {"temp": 1, "pressure": 2, "vibration": 2}
NORMAL_LOW = np.array([45.0, 1.00, 0.02])
NORMAL_HIGH = np.array([50.0, 1.05, 0.04])
STRICT_LOW = np.array([43.0, 0.97, -np.inf])
STRICT_HIGH = np.array([52.0, 1.08, 0.07])
LEVEL_NAMES = {None: "未知", 0: "正常", 1: "關注", 2: "警告", 3: "危急"}
MODEL_WATCH_PERCENTILE = 98
MODEL_WARNING_PERCENTILE = 98


def read_records(path: pathlib.Path):
    """逐筆讀取並驗證 CSV，缺值保留為 None。

    Args:
        path: 五欄感測 CSV 的路徑。

    Yields:
        包含資料列號、時間、三項原始量測與外部 label 的字典。

    Raises:
        ValueError: 欄位、量測、標籤或時間間隔不符合格式。
    """
    previous = None
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["timestamp", *SENSORS, "label"]:
            raise ValueError("CSV 必須依序包含 timestamp、temp、pressure、"
                             "vibration、label。")
        for row, raw in enumerate(reader, 1):
            try:
                if None in raw or any(value is None for value in raw.values()):
                    raise ValueError("欄位數不一致")
                timestamp = datetime.datetime.strptime(
                    raw["timestamp"].strip(), "%Y-%m-%d %H:%M:%S")
                if previous is not None and timestamp - previous != (
                        datetime.timedelta(minutes=1)):
                    raise ValueError("時間必須每分鐘遞增且不可重複")
                if raw["label"] not in ("normal", "abnormal"):
                    raise ValueError("label 必須為 normal 或 abnormal")
                record = {"row": row, "timestamp": timestamp,
                          "label": raw["label"]}
                record.update({sensor: float(raw[sensor])
                               if raw[sensor].strip() else None
                               for sensor in SENSORS})
            except (TypeError, ValueError) as error:
                raise ValueError(f"第 {row} 筆資料格式錯誤：{error}") from error
            previous = timestamp
            yield record


def initial_records(records) -> list[dict]:
    """只取前 250 筆作為初始化，尚不讀取第 251 筆。

    Args:
        records: 逐筆資料迭代器。

    Returns:
        前 250 筆記錄；迭代器停在第 251 筆之前。

    Raises:
        ValueError: 資料不足 250 筆。
    """
    baseline = list(itertools.islice(records, 250))
    if len(baseline) != 250:
        raise ValueError("初始化至少需要 250 筆資料。")
    return baseline


class Monitor:
    """管理固定模型、因果補值與逐筆風險計數。

    Args:
        baseline: 已依時間排序的前 250 筆資料。

    Raises:
        ValueError: 初始化資料不足，或有效訓練／校準量測太少。
    """

    def __init__(self, baseline: list[dict]):
        if len(baseline) != 250:
            raise ValueError("初始化必須使用前 250 筆資料。")
        values = np.array([[record[sensor] for sensor in SENSORS]
                           for record in baseline], dtype=float)
        valid = np.isfinite(values).all(axis=1) & (values != 999).all(axis=1)
        train = values[:200][valid[:200]]
        calibration = values[200:][valid[200:]]
        if len(train) < 50 or len(calibration) < 20:
            raise ValueError("基準的有效原始資料不足，無法建立模型。")
        self.model = ensemble.IsolationForest(
            n_estimators=100, contamination="auto", random_state=42)
        self.model.fit(train)
        self.calibration = np.sort(-self.model.score_samples(calibration))
        self.baseline_counts = {"initial": 250, "training": len(train),
                                "calibration": len(calibration)}
        self.last_valid = {sensor: None for sensor in SENSORS}
        self.run_value = {sensor: None for sensor in SENSORS}
        self.run_count = {sensor: 0 for sensor in SENSORS}
        self.history = collections.deque(maxlen=5)
        self.strict_count = np.zeros(3, dtype=int)
        self.model_count = self.joint_count = self.aoi_count = 0
        for record in baseline:
            self._prepare(record)

    def _prepare(self, record: dict) -> tuple:
        """只以前值補兩分鐘，原始缺值及錯誤碼仍留在紀錄。"""
        raw = np.array([record[sensor] for sensor in SENSORS], dtype=float)
        valid = np.isfinite(raw) & (raw != 999)
        effective = raw.copy()
        imputed, issues = [], []
        for index, sensor in enumerate(SENSORS):
            value = raw[index]
            if valid[index]:
                self.last_valid[sensor] = (value, record["timestamp"])
                self.run_count[sensor] = (self.run_count[sensor] + 1
                                          if self.run_value[sensor] == value
                                          else 1)
                self.run_value[sensor] = value
                # 穩態與固定解析度都會產生重複值，筆數只留作背景紀錄。
                continue
            self.run_count[sensor] = 0
            self.run_value[sensor] = None
            previous = self.last_valid[sensor]
            age = ((record["timestamp"] - previous[1]).total_seconds() / 60
                   if previous else None)
            if previous and 0 < age <= 2:
                effective[index] = previous[0]
                imputed.append(sensor)
            else:
                effective[index] = np.nan
            kind = "missing" if record[sensor] is None else "invalid"
            level = 2 if kind == "invalid" or age is None or age > 2 else 1
            text = (f"{sensor} 缺值" if kind == "missing"
                    else f"{sensor} 回傳無效數值")
            text += "，已以前值暫補" if sensor in imputed else "，無可用補值"
            issues.append({"kind": kind, "sensor": sensor, "level": level,
                           "age_minutes": age, "text": text})
        self.history.append(np.where(valid, raw, np.nan))
        return raw, valid, effective, imputed, issues

    def score(self, values: np.ndarray) -> float:
        """回傳相對校準資料的異常百分位，非故障機率。

        Args:
            values: 三個有限量測值，依 SENSORS 排序。

        Returns:
            0–100 的相對異常分數，同分採中間排名。
        """
        raw_score = -float(self.model.score_samples(values.reshape(1, -1))[0])
        left = np.searchsorted(self.calibration, raw_score, side="left")
        right = np.searchsorted(self.calibration, raw_score, side="right")
        return float(100 * (left + right) / (2 * len(self.calibration)))

    def analyze(self, record: dict) -> dict:
        """依當前量測更新歷史與連續計數，不重新訓練。

        Args:
            record: read_records 已驗證的當筆記錄，須依時間順序傳入。

        Returns:
            原始值、暫補值、分數、風險及事件線索；不修改輸入記錄。
            unchanged_samples 僅記錄連續相同的有效原始值筆數，不代表卡值。
        """
        raw, valid, effective, imputed, issues = self._prepare(record)
        provisional = bool(imputed)
        score = self.score(effective) if np.isfinite(effective).all() else None
        strict = valid & ((raw < STRICT_LOW) | (raw > STRICT_HIGH))
        gray = valid & ((raw < NORMAL_LOW) | (raw > NORMAL_HIGH))
        self.strict_count = np.where(strict, self.strict_count + 1, 0)
        self.model_count = (self.model_count + 1 if score is not None
                            and score >= MODEL_WARNING_PERCENTILE
                            and not provisional else 0)
        self.joint_count = self.joint_count + 1 if gray.sum() >= 2 else 0
        self.aoi_count = self.aoi_count + 1 if record["label"] == "abnormal" else 0
        history = np.array(self.history)
        rising = bool(len(history) == 5 and np.isfinite(history[:, 0]).all()
                      and (np.diff(history[:, 0]) >= 0).all()
                      and history[-1, 0] - history[0, 0] >= 0.4)
        reasons, kind = [], "model"
        if strict.sum() >= 2:
            level, kind = 3, "multisensor"
        elif self.strict_count.max() >= 3:
            level = 3
        elif strict.any() or self.model_count >= 3 or self.joint_count >= 3:
            level = 2
        elif gray.any() or (score is not None and score >= MODEL_WATCH_PERCENTILE
                           and not provisional):
            level = 1
        else:
            level = 0
        if strict.any():
            if kind != "multisensor":
                kind = ("thermal", "pressure", "vibration")[int(np.flatnonzero(strict)[0])]
            for index in np.flatnonzero(strict):
                sensor = SENSORS[index]
                reasons.append(f"{sensor} {raw[index]:.{DECIMALS[sensor]}f} 嚴格超標")
                if self.strict_count[index] >= 3:
                    reasons.append(f"{sensor} 已連續 {self.strict_count[index]} 筆超標")
        elif gray.sum() >= 2:
            kind = "joint"
            reasons.append(f"{int(gray.sum())} 項量測同時位於灰區"
                           f"，連續 {self.joint_count} 筆")
        elif gray[0]:
            kind = "thermal"
            reasons.append("溫度已進入灰區")
        elif gray.any():
            index = int(np.flatnonzero(gray)[0])
            kind = ("thermal", "pressure", "vibration")[index]
            reasons.append(f"{SENSORS[index]} 已進入灰區")
        if rising and gray[0]:
            reasons.append("溫度近期持續上升")
        if (score is not None and score >= MODEL_WATCH_PERCENTILE
                and not provisional):
            reasons.append("感測數值與平常不同"
                           + (f"，連續 {self.model_count} 筆"
                              if self.model_count >= 3 else ""))
        if self.aoi_count:
            if not level:
                kind = "aoi"
            level = max(level, 2 if self.aoi_count >= 3 else 1)
            reasons.append(f"外部標籤連續 {self.aoi_count} 筆 abnormal，需交叉確認")

        signals = {}
        if level:
            signals["equipment"] = {"kind": kind, "level": level,
                                     "reasons": reasons,
                                     "sensors": [SENSORS[i]
                                                 for i in np.flatnonzero(gray)]}
        for issue in issues:
            key = f"quality:{issue['kind']}:{issue['sensor']}"
            signals[key] = {"kind": issue["kind"], "level": issue["level"],
                            "reasons": [issue["text"]],
                            "sensors": [issue["sensor"]]}
        return {
            "row": record["row"],
            "timestamp": record["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
            "raw": {sensor: float(raw[i]) if np.isfinite(raw[i]) else None
                    for i, sensor in enumerate(SENSORS)},
            "effective": {sensor: float(effective[i])
                          if np.isfinite(effective[i]) else None
                          for i, sensor in enumerate(SENSORS)},
            "raw_valid": {sensor: bool(valid[i]) for i, sensor in enumerate(SENSORS)},
            "unchanged_samples": dict(self.run_count),
            "label": record["label"], "score": score,
            "score_provisional": provisional, "imputed": imputed,
            "equipment_level": level if level or valid.all() else None,
            "quality_level": max((issue["level"] for issue in issues), default=0),
            "quality": issues, "signals": signals,
            "strict_sensors": [SENSORS[i] for i in np.flatnonzero(strict)],
        }


class EventTracker:
    """合併同段事件，三筆有效正常後解除，警告以上建立工單草稿。"""

    def __init__(self):
        self.events = []
        self.active = {}
        self.work_orders = {}

    def update(self, reading: dict) -> list[dict]:
        """更新事件與工單，並保留尚未解除的設備風險。

        Args:
            reading: Monitor.analyze 的結果；設備等級可能因未解除事件而提高。

        Returns:
            本筆新增、升級或解除的事件清單。
        """
        changes = []
        for key, signal in reading["signals"].items():
            event = self.active.get(key)
            changed = None
            if event is None:
                event = {"id": f"E{len(self.events) + 1:04d}", "key": key,
                         "kind": signal["kind"], "level": signal["level"],
                         "start_row": reading["row"],
                         "start_time": reading["timestamp"],
                         "recover_count": 0}
                self.events.append(event)
                self.active[key] = event
                changed = "new"
            elif signal["level"] > event["level"]:
                changed = "upgrade"
            if signal["level"] >= event["level"]:
                event["kind"] = signal["kind"]
                event["evidence"] = list(signal["reasons"])
                event["evidence_row"] = reading["row"]
                event["sensors"] = list(signal.get("sensors", []))
            event["level"] = max(event["level"], signal["level"])
            event.update(status="active", last_row=reading["row"],
                         last_time=reading["timestamp"], recover_count=0)
            event["advice"] = advice.make_advice(event["kind"], event["sensors"])
            if changed:
                changes.append({"change": changed, "event": dict(event)})
        for key, event in list(self.active.items()):
            if key in reading["signals"]:
                continue
            valid = reading["raw_valid"]
            can_recover = (all(valid.values()) if key == "equipment"
                           else valid[key.rsplit(":", 1)[1]])
            event["recover_count"] = event["recover_count"] + 1 if can_recover else 0
            event["status"] = "recovering" if can_recover else "awaiting_data"
            if event["recover_count"] >= 3:
                event.update(status="resolved", end_row=reading["row"],
                             end_time=reading["timestamp"])
                del self.active[key]
                changes.append({"change": "resolved", "event": dict(event)})
        for event in self.events:
            if event["level"] < 2:
                continue
            previous = self.work_orders.get(event["id"], {})
            self.work_orders[event["id"]] = {
                "id": f"WO-{event['id']}", "event_id": event["id"],
                "status": "draft", "event_status": event["status"],
                "priority": "緊急" if event["level"] == 3 else "優先檢查",
                "problem": event["advice"]["title"],
                "evidence": event["evidence"][:2],
                "causes": event["advice"]["causes"],
                "actions": event["advice"]["actions"],
                "recommendation": event["advice"]["recommendation"],
                "created_at": previous.get("created_at", reading["timestamp"]),
                "updated_at": (reading["timestamp"] if event["status"] != "resolved"
                               else event["end_time"]),
            }
        active = self.active.get("equipment")
        if active:
            reading["equipment_level"] = max(
                reading["equipment_level"] or 0, active["level"])
        return changes
