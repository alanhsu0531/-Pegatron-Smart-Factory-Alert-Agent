"""繪製終端原始量測趨勢；缺值與無效回報不連線、不使用補值。"""

import math

import plotext
from rich import console
from rich import rule
from rich import table
from rich import text

import monitor
import theme


WINDOW_SAMPLES = 60
SENSOR_NAMES = {"temp": "溫度（°C）", "pressure": "壓力", "vibration": "振動"}
PIXELS = {name: plotext.pixel(foreground=tuple(bytes.fromhex(color[1:])))
          for name, color in theme.PALETTE.items()}
plotext.add_theme("factory", text=PIXELS["muted"], sequence=[PIXELS["signal"]])


def sensor_chart(records: list[dict], sensor: str, width: int,
                 height: int = 8) -> text.Text:
    """繪製單一感測器，嚴格邊界與超標值使用不同標記。

    Args:
        records: 截至當前時點、依時間排序的原始資料，不含補值。
        sensor: 感測器欄位名稱。
        width: 圖表字元寬度。
        height: 圖表列數。

    Returns:
        可交由 Rich 顯示的彩色圖表文字；無效資料會留下斷點。
    """
    index = monitor.SENSORS.index(sensor)
    lower, upper = monitor.STRICT_LOW[index], monitor.STRICT_HIGH[index]
    boundaries = [value for value in (lower, upper) if math.isfinite(value)]
    points = [(i, row[sensor]) for i, row in enumerate(records)
              if row[sensor] is not None and math.isfinite(row[sensor])
              and row[sensor] != 999]
    xs = [i for i, _ in points]
    ys = [value for _, value in points]
    low = lower if math.isfinite(lower) else 0.0
    padding = (upper - low) * 0.15
    bottom, top = min([low, *ys]), max([upper, *ys])
    ticks = sorted(set([*boundaries, bottom, top]))
    decimals = monitor.DECIMALS[sensor]

    plotext.terminal.limit(False, False)
    figure = plotext.figure.clear().plot_size(width, height).theme("factory")
    figure.ruler("y").lim(bottom - padding, top + padding).ticks(
        ticks, labels=[f"{value:.{decimals}f}" for value in ticks])
    end = max(1, len(records) - 1)
    positions = [0, len(records) - 1] if len(records) > 1 else [0]
    figure.ruler("x").lim(0, end).ticks(
        positions, labels=[records[i]["timestamp"].strftime("%H:%M")
                           for i in positions])
    for boundary in boundaries:
        # line() 預設會補白底，改用前景色線段以繼承終端背景。
        figure.draw(figure.signal(
            [0, end], [boundary, boundary],
            marker=plotext.marker("┈", pixel=PIXELS["danger"])).lines(True))
    if points:
        signal = figure.signal(
            xs, ys, marker=plotext.marker("braille", pixel=PIXELS["signal"]))
        signal.lines(True)
        for i in range(1, len(xs)):
            if xs[i] != xs[i - 1] + 1:
                signal.line(i, False)
        figure.draw(signal)
        abnormal = [(i, value) for i, value in points
                    if value < lower or value > upper]
        if abnormal:
            figure.draw(figure.signal(
                [i for i, _ in abnormal], [value for _, value in abnormal],
                marker=plotext.marker("●", pixel=PIXELS["attention"])))
    return text.Text.from_ansi(figure.build().string().rstrip("\n"))


def trend_charts(records, width: int, height: int = 8):
    """並排顯示三個不同尺度、相同時間範圍的近期趨勢。

    Args:
        records: 僅含已讀原始資料的歷史窗口。
        width: 終端可用字元寬度。
        height: 各圖表列數，不含標題與圖例。

    Returns:
        包含三張圖及共用圖例的 Rich 顯示群組。
    """
    records = list(records)[-WINDOW_SAMPLES:]
    columns = table.Table.grid(padding=(0, 1))
    widths = [(width - 4) // 3] * 2
    widths.append(width - 4 - sum(widths))
    plots = []
    for sensor, column_width in zip(monitor.SENSORS, widths):
        columns.add_column(width=column_width, min_width=column_width,
                           max_width=column_width)
        index = monitor.SENSORS.index(sensor)
        decimals = monitor.DECIMALS[sensor]
        lower, upper = monitor.STRICT_LOW[index], monitor.STRICT_HIGH[index]
        limits = (f"{lower:.{decimals}f}–{upper:.{decimals}f}"
                  if math.isfinite(lower) else f"上限 {upper:.{decimals}f}")
        plots.append(console.Group(
            text.Text.assemble((SENSOR_NAMES[sensor], "bold"),
                               (f"  {limits}", theme.PALETTE["muted"])),
            sensor_chart(records, sensor, column_width, height)))
    columns.add_row(*plots)
    return console.Group(
        rule.Rule("近一小時趨勢", align="left", style=theme.PALETTE["border"]),
        columns,
        text.Text.from_markup(
            f"[{theme.PALETTE['signal']}]━━ 原始量測[/]   "
            f"[{theme.PALETTE['danger']}]┈┈ 紅虛線：嚴格邊界[/]   "
            f"[{theme.PALETTE['attention']}]● 超標[/]   "
            f"[{theme.PALETTE['muted']}]斷點：缺值／無效[/]"))
