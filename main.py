"""以精簡終端畫面播放 CSV，輸出本機事件與工單草稿。"""

import argparse
import collections
import json
import math
import pathlib
import sqlite3
import sys
import time

from rich import box
from rich import console
from rich import live
from rich import panel
from rich import table
from rich import text

from factory_agent import advice
from factory_agent import chat
from factory_agent import history_store
from factory_agent import local_agent
from factory_agent import monitor
from factory_agent import result_view
from factory_agent import theme
from factory_agent import trends


PALETTE = theme.PALETTE
COLORS = {None: PALETTE["muted"], 0: PALETTE["normal"],
          1: PALETTE["attention"], 2: PALETTE["attention"],
          3: f"bold {PALETTE['danger']}"}
STATUS_NAMES = {"active": "持續中", "recovering": "恢復觀察中",
                "awaiting_data": "等待有效資料", "resolved": "已恢復"}
SENSOR_NAMES = {"temp": "溫度（°C）", "pressure": "壓力", "vibration": "振動"}


def value_region(sensor: str, value, is_valid: bool) -> tuple[str, str]:
    """依原始回報取得作業條件文字與顏色。

    Args:
        sensor: 感測器欄位名稱。
        value: 原始回報值，缺值為 None。
        is_valid: 資料品質檢查判定該回報是否有效。

    Returns:
        區間描述與 Rich 顏色；無效值不套用設備門檻。
    """
    if value is None:
        return "未回報", PALETTE["muted"]
    if not is_valid:
        return "無效數值", PALETTE["danger"]
    index = monitor.SENSORS.index(sensor)
    if value < monitor.STRICT_LOW[index]:
        return "低於下限", PALETTE["danger"]
    if value > monitor.STRICT_HIGH[index]:
        return "超過上限", PALETTE["danger"]
    if value < monitor.NORMAL_LOW[index]:
        return "灰區偏低", PALETTE["attention"]
    if value > monitor.NORMAL_HIGH[index]:
        return "灰區偏高", PALETTE["attention"]
    return "正常範圍", PALETTE["normal"]


def guidance(tracker: monitor.EventTracker, compact: bool = False, width: int = 80):
    """將最高優先事件轉成簡短的現場處置提示。"""
    if not tracker.active:
        detail = "目前沒有持續中的提醒，請持續觀察。"
        if tracker.work_orders and not compact:
            detail += f"\n另有 {len(tracker.work_orders)} 份工單草稿待確認。"
        return text.Text(detail, style=PALETTE["muted"]) if compact else panel.Panel(
            detail, title="處置重點", title_align="left", box=box.SQUARE,
            border_style=PALETTE["border"])
    event = max(tracker.active.values(), key=lambda item: item["level"])
    recommendation = event["advice"]
    if (event["status"] == "active"
            and all(field in recommendation for field, _ in advice.CONTEXT_FIELDS)):
        detail = text.Text()
        as_of = recommendation["as_of"][11:16]
        fields = (("situation", f"{as_of} 狀況"), ("first_action", "先做"),
                  ("next_step", "查核後")) if compact else advice.CONTEXT_FIELDS
        for index, (field, label) in enumerate(fields):
            value = (recommendation["actions"][0] if field == "first_action"
                     else recommendation[field])
            line = text.Text.assemble((f"{label}：", "bold"), value)
            if compact:
                line.truncate(width, overflow="ellipsis")
            if index:
                detail.append("\n")
            detail.append_text(line)
        if compact:
            return detail
        if event["id"] in tracker.work_orders:
            detail.append(f"\n工單 WO-{event['id']}：已備妥草稿，待確認。")
        if len(tracker.active) > 1:
            detail.append(f"\n另有 {len(tracker.active) - 1} 項提醒，詳情已記錄。")
        return panel.Panel(detail, title=f"處置重點 · 依據 {as_of} 回報",
                           title_align="left", box=box.SQUARE,
                           border_style=COLORS[event["level"]])
    if event["status"] == "recovering":
        detail = ("[bold]目前狀況：[/]正在確認恢復，提醒尚未解除。\n"
                  f"[bold]下一步：[/]再確認 {3 - event['recover_count']} 筆有效回報。")
    elif event["status"] == "awaiting_data":
        detail = ("[bold]目前狀況：[/]資料不足，先前提醒暫時保留。\n"
                  "[bold]下一步：[/]先恢復有效回報，再確認設備狀態。")
    else:
        detail = f"[bold]目前狀況：[/]{recommendation['title']}\n[bold]建議處置：[/]"
        if compact:
            detail += recommendation["actions"][0]
        else:
            detail += "\n" + "\n".join(
                f"  {i}. {action}" for i, action in enumerate(recommendation["actions"], 1))
            detail += (f"\n[{PALETTE['muted']}]優先檢查方向："
                       f"{recommendation['causes'][0]}[/]")
    if event["id"] in tracker.work_orders and not compact:
        detail += f"\n工單 WO-{event['id']}：已備妥草稿，待確認。"
    if len(tracker.active) > 1 and not compact:
        detail += f"\n另有 {len(tracker.active) - 1} 項提醒，詳情已記錄。"
    return (text.Text.from_markup(detail) if compact
            else panel.Panel(detail, title="處置重點", title_align="left",
                             box=box.SQUARE, border_style=COLORS[event["level"]]))


def dashboard(reading: dict, tracker: monitor.EventTracker, width: int = 80,
              history=(), height: int = 40):
    """呈現狀態及近期趨勢；小視窗保留優先處置，欄寬不受內容影響。"""
    equipment = reading["equipment_level"]
    equipment_name = monitor.LEVEL_NAMES[equipment]
    quality = monitor.LEVEL_NAMES[reading["quality_level"]]
    aoi_normal = reading["label"] == "normal"
    aoi_name = "正常" if aoi_normal else "異常"
    aoi_color = PALETTE["normal"] if aoi_normal else PALETTE["attention"]
    if reading["score"] is None:
        model_status = "運轉觀察：資料不足，暫時無法判斷"
    elif reading["score_provisional"]:
        model_status = "運轉觀察：部分資料暫補中，等待新回報確認"
    elif reading["score"] >= monitor.MODEL_WATCH_PERCENTILE:
        model_status = "運轉觀察：與平常不同"
    else:
        model_status = "運轉觀察：與平常相近"
    compact = bool(history) and height < 36
    status = (f"[{COLORS[equipment]}]設備綜合評估：{equipment_name}[/]  "
              f"AOI設備監測：[{aoi_color}]{aoi_name}[/]  "
              f"資料品質：[{COLORS[reading['quality_level']]}]{quality}[/]\n"
              f"[{PALETTE['muted']}]{model_status}[/]")
    # 四欄共五條框線、八格左右留白；其餘寬度固定分配。
    available = max(4, width - 13)
    widths = [max(1, int(available * ratio)) for ratio in (0.20, 0.16, 0.20)]
    widths.append(available - sum(widths))
    values = table.Table(width=available + 13, box=box.SQUARE,
                         border_style=PALETTE["border"],
                         header_style=f"bold {PALETTE['muted']}")
    for heading, column_width in zip(
            ("感測器", "原始回報", "作業條件", "回報狀態"), widths):
        values.add_column(heading, justify="right" if heading == "原始回報" else "left",
                          width=column_width, min_width=column_width,
                          max_width=column_width, overflow="fold")
    for sensor in monitor.SENSORS:
        raw = reading["raw"][sensor]
        value = "缺值" if raw is None else f"{raw:.{monitor.DECIMALS[sensor]}f}"
        issues = [item for item in reading["quality"] if item["sensor"] == sensor]
        if sensor in reading["imputed"]:
            note = "前值暫補，保留原始異常"
        elif any(item["kind"] in ("missing", "invalid") for item in issues):
            note = "不可用"
        else:
            note = "有效回報"
        region, color = value_region(sensor, raw, reading["raw_valid"][sensor])
        note_color = PALETTE["muted"] if note == "有效回報" else PALETTE["attention"]
        values.add_row(SENSOR_NAMES[sensor], text.Text(value, style="bold"),
                       text.Text(region, style=color), text.Text(note, style=note_color))
    overall = max(equipment or 0, reading["quality_level"])
    charts = [trends.trend_charts(history, width, 7 if compact else 8)] if history else []
    heading = table.Table.grid(expand=True)
    heading.add_column(style=f"bold {PALETTE['signal']}")
    heading.add_column(justify="right", style=PALETTE["muted"])
    heading.add_row("智慧工廠設備預警", reading["timestamp"])
    header = (text.Text.from_markup(status) if compact else panel.Panel(
        status, box=box.SQUARE, border_style=PALETTE["border"] if not overall
        else COLORS[overall]))
    footer = table.Table.grid(expand=True)
    footer.add_column(style=PALETTE["attention"] if tracker.work_orders else PALETTE["muted"])
    footer.add_column(justify="right", style=PALETTE["muted"])
    source = getattr(tracker, "agent_status", "本機規則模板")
    if tracker.active:
        current = max(tracker.active.values(), key=lambda event: event["level"])
        source = ("規則建議 · AI 整理中" if current["id"] in
                  getattr(tracker, "agent_pending", ()) else current["advice"]["source"])
    footer.add_row(f"工單草稿 {len(tracker.work_orders)} 份 · 待確認", source)
    return console.Group(
        heading, header, values, *charts, guidance(tracker, compact, width), footer,
    )


def save_results(output_dir: pathlib.Path, tracker: monitor.EventTracker,
                 summary: dict) -> None:
    """儲存本機紀錄與使用者結果頁；告警恢復不代表工單維修完成。"""
    for name, value in [("events.json", tracker.events),
                        ("work_orders.json", list(tracker.work_orders.values())),
                        ("summary.json", summary)]:
        (output_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8")
    result_view.write_results_page(output_dir / "results.html", summary,
                                   tracker.events, list(tracker.work_orders.values()))


def run(csv_path: pathlib.Path, output_dir: pathlib.Path, interval: float,
        output: console.Console, use_ollama: bool = False) -> dict:
    """先初始化 250 筆，再依序監測；原始 CSV 只讀。

    Args:
        csv_path: 感測資料來源，不載入生成註記。
        output_dir: 本次事件與工單紀錄目錄。
        interval: 每筆播放間隔秒數，0 代表快速驗收。
        output: Rich 終端輸出。
        use_ollama: 啟用地端建議；程式呼叫預設使用模板，CLI 預設啟用。

    Returns:
        監測摘要。

    Raises:
        ValueError: CSV 格式或筆數不符。
        OSError: 無法讀取資料或儲存紀錄。
    """
    records = monitor.read_records(csv_path)
    try:
        baseline = monitor.initial_records(records)
        detector = monitor.Monitor(baseline)
        output.print(f"[bold {PALETTE['signal']}]初始化完成[/]  開始監測設備回報")
        tracker = monitor.EventTracker()
        history = collections.deque(baseline[-trends.WINDOW_SAMPLES:],
                                    maxlen=trends.WINDOW_SAMPLES)
        output_dir.mkdir(parents=True, exist_ok=True)
        store = history_store.HistoryStore(output_dir)
        counts = collections.Counter()
        missing = collections.Counter()
        imputed = collections.Counter()
        last = None
        first_timestamp = None
        interrupted = False
        completed = False
        with (output_dir / "readings.jsonl").open("w", encoding="utf-8") as stream, (
                (output_dir / "agent_trace.jsonl").open("w", encoding="utf-8")) as trace_stream:
            agent = local_agent.LocalAgent(trace_stream) if use_ollama else None
            if agent:
                tracker.agent_status = "地端 AI 待命"
            with live.Live(console=output, auto_refresh=False) as screen:
                try:
                    for record in records:
                        if record["row"] > 500:
                            raise ValueError("本專案 CSV 預期為 500 筆。")
                        reading = detector.analyze(record)
                        tracker.update(reading)
                        history.append(record)
                        if agent:
                            agent.update(tracker, reading, history)
                        counts[monitor.LEVEL_NAMES[reading["equipment_level"]]] += 1
                        missing.update(sensor for sensor in monitor.SENSORS
                                       if record[sensor] is None)
                        imputed.update(reading["imputed"])
                        stream.write(json.dumps(reading, ensure_ascii=False,
                                                allow_nan=False) + "\n")
                        stream.flush()
                        last = reading
                        if first_timestamp is None:
                            first_timestamp = reading["timestamp"]
                        if output.is_terminal:
                            screen.update(dashboard(reading, tracker, output.width,
                                                    history, output.height),
                                          refresh=True)
                        if interval:
                            time.sleep(interval)
                    completed = last is not None and last["row"] == 500
                except KeyboardInterrupt:
                    interrupted = True
                finally:
                    if agent:
                        try:
                            agent.close(tracker, wait=not interrupted)
                        except KeyboardInterrupt:
                            interrupted = True
                            agent.close(tracker, wait=False)
                    summary = {
                        "status": ("interrupted" if interrupted else
                                   "finished" if completed else "incomplete"),
                        "initial_rows": 250, "monitored_rows": sum(counts.values()),
                        "baseline": detector.baseline_counts,
                        "equipment_level_counts": dict(counts),
                        "missing_by_sensor": dict(missing),
                        "imputed_by_sensor": dict(imputed),
                        "events": len(tracker.events),
                        "event_counts_by_kind": dict(collections.Counter(
                            event["kind"] for event in tracker.events)),
                        "work_orders": len(tracker.work_orders),
                        "active_events": len(tracker.active),
                        "last_timestamp": last["timestamp"] if last else None,
                        "first_timestamp": first_timestamp,
                        "last_state": {key: last[key] for key in
                                       ("equipment_level", "quality_level", "label")}
                        if last else None,
                        "advice_source": getattr(tracker, "agent_status", "本機規則模板"),
                        "agent": agent.summary() if agent else {"enabled": False},
                    }
                    summary["run_id"] = store.archive(
                        summary, tracker.events, list(tracker.work_orders.values()))
                    save_results(output_dir, tracker, summary)
        if not interrupted and (last is None or last["row"] != 500):
            raise ValueError("本專案 CSV 預期共 500 筆，資料已提前結束。")
        output.print(f"\n[bold {PALETTE['normal']}]監測完成[/]" if not interrupted
                     else f"\n[{PALETTE['attention']}]已停止播放，紀錄已保存[/]")
        output.print(f"逐筆監測 {summary['monitored_rows']} 筆 · "
                     f"保留缺值 {sum(missing.values())} 格 · "
                     f"工單草稿 {len(tracker.work_orders)} 份")
        events = table.Table(
            table.Column("時間", width=5, no_wrap=True),
            table.Column("事件摘要", ratio=1, overflow="fold"),
            table.Column("最高風險", width=8),
            table.Column("狀態", width=12),
            table.Column("工單草稿", width=8),
            box=box.SQUARE, border_style=PALETTE["border"],
            header_style=f"bold {PALETTE['muted']}", expand=True)
        important = [event for event in tracker.events
                     if event["level"] >= 2 or event["kind"] == "aoi"]
        for event in important[-10:]:
            work_order = (f"WO-{event['id']}" if event["id"] in tracker.work_orders
                          else "—")
            events.add_row(event["start_time"][11:16], event["advice"]["title"],
                           text.Text(monitor.LEVEL_NAMES[event["level"]],
                                     style=COLORS[event["level"]]),
                           STATUS_NAMES[event["status"]], work_order)
        output.print(events)
        contextual = [order for order in tracker.work_orders.values()
                      if all(field in order for field, _ in advice.CONTEXT_FIELDS)]
        if contextual:
            order = min(contextual, key=lambda item: (item["priority"] != "緊急",
                                                       item["created_at"]))
            detail = text.Text()
            for index, (field, label) in enumerate(advice.CONTEXT_FIELDS):
                if index:
                    detail.append("\n")
                detail.append(f"{label}：", style="bold")
                detail.append(order[field])
            output.print(panel.Panel(
                detail, title=f"優先工單建議 · {order['id']}", title_align="left",
                subtitle=f"告警{STATUS_NAMES[order['event_status']]} · 草稿待確認 · "
                         f"依據 {order['as_of'][11:16]} 回報",
                box=box.SQUARE, border_style=PALETTE["border"]))
        output.print("工單保留為待確認草稿；告警恢復不代表檢修已完成。")
        output.print(f"結果摘要與維修工單：{output_dir / 'results.html'}", markup=False)
        output.print(f"詳細逐筆紀錄：{output_dir}", markup=False, style=PALETTE["muted"])
        output.print(f"建議來源：{summary['advice_source']}", markup=False,
                     style=PALETTE["muted"])
        return summary
    finally:
        records.close()


def main() -> None:
    """啟動本機終端 Demo，依序讀取 CSV 並保留原始資料。"""
    root = pathlib.Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="智慧工廠設備預警終端 Demo")
    parser.add_argument("--csv", type=pathlib.Path,
                        default=root / "data" / "sensor_data.csv")
    parser.add_argument("--output-dir", type=pathlib.Path, default=root / "outputs")
    parser.add_argument("--interval", type=float, default=0.5,
                        help="每筆間隔秒數；0 可快速驗收")
    parser.add_argument("--no-ollama", action="store_true",
                        help="僅使用本機規則模板，不連線 Ollama")
    parser.add_argument("--chat", action="store_true",
                        help="直接開啟已保存監測紀錄的互動問答")
    args = parser.parse_args()
    output = console.Console()
    if not math.isfinite(args.interval) or args.interval < 0:
        parser.error("播放間隔必須為大於或等於 0 的有限數值。")
    if args.chat and args.no_ollama:
        parser.error("互動問答需要 Ollama，不能同時使用 --no-ollama。")
    try:
        if args.chat:
            chat.run_chat(history_store.HistoryStore(args.output_dir), output)
            return
        summary = run(args.csv, args.output_dir, args.interval, output,
                      use_ollama=not args.no_ollama)
        if (not args.no_ollama and summary["status"] == "finished"
                and output.is_terminal and sys.stdin.isatty()):
            chat.run_chat(history_store.HistoryStore(args.output_dir), output)
    except (OSError, ValueError, sqlite3.Error) as error:
        output.print("無法完成監測：", str(error), style="red", markup=False)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
