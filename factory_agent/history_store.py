"""將監測批次與人工確認的工單修訂保存在單一本機 SQLite 檔案。"""

import contextlib
import datetime
import json
import pathlib
import re
import sqlite3
import uuid

from factory_agent import advice
from factory_agent import monitor
from factory_agent import result_view


EDIT_FIELDS = {"problem": "問題摘要", "description": "工單說明", "notes": "現場備註"}


def now() -> str:
    """取得台北時間，與感測資料時間分開保存。"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(timespec="seconds")


class HistoryStore:
    """保留不可覆寫的批次快照；修訂只新增版本，不修改告警證據。"""

    def __init__(self, root: pathlib.Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "history.sqlite3"
        with self.database() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, saved_at TEXT, snapshot TEXT)")
            connection.execute("""CREATE TABLE IF NOT EXISTS revisions (
                run_id TEXT, order_id TEXT, version INTEGER, saved_at TEXT,
                request TEXT, changes TEXT, PRIMARY KEY (run_id, order_id, version))""")
        # 第一次啟用時先接住既有輸出，避免下一次播放把唯一歷史覆蓋。
        if not self.runs() and all((root / name).is_file() for name in
                                   ("summary.json", "events.json", "work_orders.json")):
            summary = self._read("summary.json")
            summary["run_id"] = self.archive(summary, self._read("events.json"),
                                               self._read("work_orders.json"))
            (root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")

    @contextlib.contextmanager
    def database(self):
        """以交易執行讀寫，並確實關閉連線。"""
        connection = sqlite3.connect(self.path)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _read(self, filename: str):
        return json.loads((self.root / filename).read_text(encoding="utf-8"))

    def archive(self, summary: dict, events: list, orders: list) -> str:
        """保存已讀資料與自動工單快照，不讀取 CSV 未來資料。

        Args:
            summary: 本批執行狀態與監測區間。
            events: 已發生的事件。
            orders: 自動產生的工單草稿。

        Returns:
            此批唯一識別碼，事件與工單編號只在該批內有效。
        """
        run_id = "R" + uuid.uuid4().hex[:12]
        summary = {**summary, "run_id": run_id}
        path = self.root / "readings.jsonl"
        readings = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        if len(readings) != summary["monitored_rows"]:
            raise ValueError("監測紀錄筆數與摘要不符，歷史未保存。")
        snapshot = {"summary": summary, "events": events, "orders": orders, "readings": readings}
        with self.database() as connection:
            connection.execute("INSERT INTO runs VALUES (?, ?, ?)",
                               (run_id, now(), json.dumps(snapshot, ensure_ascii=False, allow_nan=False)))
        return run_id

    def runs(self) -> list:
        """列出最近十批執行；各批可能是同一份合成資料的重播。"""
        with self.database() as connection:
            rows = connection.execute("SELECT id, saved_at, snapshot FROM runs ORDER BY rowid DESC LIMIT 10").fetchall()
        result = []
        for run_id, saved_at, snapshot in rows:
            summary = json.loads(snapshot)["summary"]
            result.append({"run_id": run_id, "saved_at": saved_at,
                           **{key: summary.get(key) for key in
                              ("status", "first_timestamp", "last_timestamp", "monitored_rows")}})
        return result

    def load(self, run_id: str = "") -> dict:
        """讀取指定批次，套用已確認修訂；不改動資料庫內的原始快照。"""
        with self.database() as connection:
            row = (connection.execute("SELECT id, snapshot FROM runs WHERE id = ?", (run_id,)).fetchone()
                   if run_id else connection.execute("SELECT id, snapshot FROM runs ORDER BY rowid DESC LIMIT 1").fetchone())
            if row is None:
                raise ValueError("沒有這個監測批次，請先完成一次監測。")
            run_id, encoded = row
            snapshot = json.loads(encoded)
            versions = connection.execute(
                "SELECT order_id, version, saved_at, changes FROM revisions WHERE run_id = ? ORDER BY version",
                (run_id,)).fetchall()
        by_id = {order["id"]: order for order in snapshot["orders"]}
        for order in by_id.values():
            order["revision"] = 0
        for order_id, version, saved_at, encoded in versions:
            order = by_id[order_id]
            changes = json.loads(encoded)
            if "problem" in changes:
                order["problem"] = changes["problem"]
            if "description" in changes:
                order["operator_description"] = changes["description"]
            if "notes" in changes:
                order["operator_notes"] = changes["notes"]
            order.update(revision=version, revised_at=saved_at)
        return snapshot

    @staticmethod
    def editable(order: dict) -> dict:
        """取得三個可編修欄位，不包含風險、優先程度、證據與告警狀態。"""
        return {"problem": order["problem"],
                "description": order.get("operator_description", order.get("recommendation", "")),
                "notes": order.get("operator_notes", "")}

    def propose(self, run_id: str, order_id: str, expected_revision: int,
                changes: dict, request: str) -> dict:
        """建立修改預覽；此方法不寫入資料。

        Args:
            run_id: 工單所屬的監測批次。
            order_id: 批次內的工單編號。
            expected_revision: 使用者正在檢視的版本。
            changes: 僅含問題摘要、說明或備註的文字修改。
            request: 保留使用者原始要求，供確認儲存後查核。

        Returns:
            修改前後內容與版本，不包含可變更的風險或證據欄位。

        Raises:
            ValueError: 工單不存在、版本已變或欄位不允許修改。
        """
        snapshot = self.load(run_id)
        order = next((item for item in snapshot["orders"] if item["id"] == order_id), None)
        if order is None:
            raise ValueError("找不到這張工單。")
        if type(expected_revision) is not int or expected_revision != order["revision"]:
            raise ValueError("工單已有新版本，請重新查詢後再修改。")
        if not changes or set(changes) - set(EDIT_FIELDS):
            raise ValueError("只能修改問題摘要、工單說明與現場備註。")
        for field, value in changes.items():
            limit = 100 if field == "problem" else 800
            if (not isinstance(value, str) or len(value) > limit
                    or (field != "notes" and not value.strip())
                    or any(ord(char) < 32 and char != "\n" for char in value)):
                raise ValueError("修改文字空白、過長或包含控制字元。")
        before = self.editable(order)
        changes = {key: value.strip() for key, value in changes.items() if value.strip() != before[key]}
        if not changes:
            raise ValueError("修改內容與目前工單相同。")
        return {"run_id": snapshot["summary"]["run_id"], "order_id": order_id,
                "expected_revision": expected_revision, "before": before,
                "changes": changes, "request": request, "priority": order["priority"]}

    def save(self, proposal: dict) -> dict:
        """使用者確認後保存新版本；拒絕過期預覽，保留完整修訂紀錄。"""
        checked = self.propose(proposal["run_id"], proposal["order_id"],
                               proposal["expected_revision"], proposal["changes"], proposal["request"])
        version = checked["expected_revision"] + 1
        try:
            with self.database() as connection:
                connection.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?)", (
                    checked["run_id"], checked["order_id"], version, now(), checked["request"],
                    json.dumps(checked["changes"], ensure_ascii=False)))
        except sqlite3.IntegrityError as error:
            raise ValueError("工單已有新版本，請重新查詢後再修改。") from error
        snapshot = self.load(checked["run_id"])
        destination = self.root / "history" / checked["run_id"]
        result = {"version": version, "path": str(destination / "results.html")}
        try:
            destination.mkdir(parents=True, exist_ok=True)
            result_view.write_results_page(destination / "results.html", snapshot["summary"],
                                           snapshot["events"], snapshot["orders"])
            latest = self._read("summary.json") if (self.root / "summary.json").exists() else {}
            if latest.get("run_id") == checked["run_id"]:
                (self.root / "work_orders.json").write_text(
                    json.dumps(snapshot["orders"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                result_view.write_results_page(self.root / "results.html", snapshot["summary"],
                                               snapshot["events"], snapshot["orders"])
        except OSError:
            result["path"] = None
        return result

    def handoff(self, run_id: str = "") -> dict:
        """由程式計算交班數字，告警恢復與工單待確認分開計數。"""
        snapshot = self.load(run_id)
        events, orders = snapshot["events"], snapshot["orders"]
        return {"summary": snapshot["summary"],
                "event_counts": {monitor.LEVEL_NAMES[level]: sum(event["level"] == level for event in events)
                                 for level in (1, 2, 3)},
                "resolved": sum(event["status"] == "resolved" for event in events),
                "pending_orders": [{"id": order["id"], "problem": order["problem"],
                                    "priority": order["priority"], "revision": order["revision"],
                                    "next_action": order.get("operator_description", order["actions"][0]).splitlines()[0],
                                    "notes": order.get("operator_notes", "")} for order in orders],
                "note": "告警恢復不代表維修完成；所有工單仍為草稿待確認，無維修完成紀錄。"}

    def query(self, run_id: str = "", keyword: str = "") -> dict:
        """查詢已保存的事件與工單，詳細時間線只取已讀紀錄。

        Args:
            run_id: 批次識別碼，空白表示最近一次。
            keyword: 事件／工單編號、感測器名稱或 HH:MM。

        Returns:
            事件與工單列表，以及單一事件的關鍵時間線與工單全文。
        """
        if not isinstance(keyword, str) or len(keyword) > 80:
            raise ValueError("請用簡短關鍵字或事件、工單編號查詢。")
        snapshot = self.load(run_id)
        keyword = keyword.strip(' \"\'“”‘’，,。')
        identifier = re.search(r"(?<![0-9A-Za-z])(?:WO-)?(E\d{4})(?!\d)", keyword, re.IGNORECASE)
        if identifier:
            keyword = identifier.group(1).upper()
        event_id = keyword
        events = []
        for event in snapshot["events"]:
            searchable = " ".join([event["kind"], event["advice"]["title"],
                                    monitor.LEVEL_NAMES[event["level"]], event["start_time"],
                                    event.get("end_time", ""), *event.get("sensors", []),
                                    *[advice.SENSOR_NAMES.get(sensor, sensor) for sensor in
                                      event.get("sensor_history", event.get("sensors", []))],
                                    *event.get("peak_evidence", [])])
            time_match = (re.fullmatch(r"\d{2}:\d{2}", keyword)
                          and event["start_time"][11:16] <= keyword <=
                          event.get("end_time", snapshot["summary"]["last_timestamp"])[11:16])
            if (not keyword or event["id"] == event_id or keyword.lower() in searchable.lower()
                    or time_match):
                events.append(event)
        orders = [order for order in snapshot["orders"]
                  if order["event_id"] in {event["id"] for event in events}]
        details = []
        if len(events) == 1:
            event = events[0]
            readings = [reading for reading in snapshot["readings"]
                        if event["start_row"] <= reading["row"] <=
                        event.get("end_row", snapshot["readings"][-1]["row"])]
            markers = {}
            for reading in readings:
                signal = reading["signals"].get(event["key"], {})
                if signal.get("level", 0) >= 2:
                    markers.setdefault("first_warning", reading)
                if signal.get("level", 0) == 3:
                    markers.setdefault("first_critical", reading)
                if reading["strict_sensors"] and event["key"] == "equipment":
                    markers.setdefault("first_strict", reading)
                reasons = reading["signals"].get(event["key"], {}).get("reasons", [])
                if any("感測數值與平常不同，連續" in reason for reason in reasons):
                    markers.setdefault("first_model_warning", reading)
            for name, reading in markers.items():
                details.append({"milestone": name, "timestamp": reading["timestamp"],
                                "raw": reading["raw"], "aoi": reading["label"],
                                "evidence": reading["signals"].get(event["key"], {}).get("reasons", [])})
        lead = None
        if len(events) == 1 and {"first_model_warning", "first_strict"} <= set(markers):
            lead = max(0, int((datetime.datetime.fromisoformat(markers["first_strict"]["timestamp"]) -
                               datetime.datetime.fromisoformat(markers["first_model_warning"]["timestamp"])).total_seconds() / 60))
        return {"run_id": snapshot["summary"]["run_id"], "summary": snapshot["summary"],
                "matching_events": len(events),
                "events": [{"id": event["id"], "title": event["advice"]["title"],
                            "peak_level": monitor.LEVEL_NAMES[event["level"]],
                            "status": event["status"], "start_time": event["start_time"],
                            "end_time": event.get("end_time"), "evidence": event["evidence"][:3],
                            "peak_evidence": event.get("peak_evidence", event["evidence"])[:3]}
                           for event in events[:20]],
                "timeline": details, "model_warning_lead_minutes": lead,
                "orders": [{"id": order["id"], "event_id": order["event_id"],
                            "priority": order["priority"], "revision": order["revision"],
                            **{key: value for key, value in self.editable(order).items()
                               if key != "description" or len(orders) == 1}} for order in orders[:20]],
                "matching_orders": len(orders),
                "runs": self.runs(),
                "note": "批次可能是同一份合成資料重播，不能把跨批事件總數當成實際故障次數。"}
