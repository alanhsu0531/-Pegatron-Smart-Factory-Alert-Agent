"""將監測結果轉為機台負責人可離線閱讀的繁體中文頁面。"""

import html
import pathlib

from factory_agent import advice
from factory_agent import monitor


STATUS_NAMES = {"active": "持續中", "recovering": "恢復觀察中",
                "awaiting_data": "等待有效回報", "resolved": "已恢復"}
RUN_NAMES = {"finished": "監測完成", "interrupted": "已停止播放",
             "incomplete": "資料未完整讀取"}
CSS = """
:root { color-scheme: light; --paper:#fff; --ink:#21313f; --muted:#607381;
        --line:#d5dfe6; --accent:#236c86; --danger:#b54043; }
* { box-sizing:border-box; }
body { margin:0; background:var(--paper); color:var(--ink);
       font:16px/1.65 -apple-system,BlinkMacSystemFont,"PingFang TC","Microsoft JhengHei",sans-serif; }
main { max-width:1100px; margin:auto; padding:32px; }
header { padding-bottom:20px; border-bottom:3px solid var(--accent); }
h1 { margin:0 0 8px; font-size:1.8rem; }
h2 { margin:32px 0 12px; font-size:1.25rem; }
p { margin:8px 0; }
.muted,dt { color:var(--muted); }
.operator-text { white-space:pre-wrap; }
.status { display:flex; flex-wrap:wrap; gap:12px 32px; margin:20px 0; }
.status div { min-width:130px; }
dt { font-size:.9rem; }
dd { margin:0; font-weight:600; }
.notice { padding:12px 16px; border-left:4px solid var(--accent); }
.notice strong { font-size:1.1rem; }
nav { display:flex; gap:24px; margin-top:16px; }
a { color:var(--accent); text-underline-offset:3px; }
.order { padding:16px 0; border-top:1px solid var(--line); }
.order summary { cursor:pointer; }
.order summary strong { display:inline; }
.order .priority { display:inline-block; margin-right:12px; color:var(--accent); font-weight:600; }
.order .urgent,.risk-2,.risk-3 { color:var(--danger); }
.risk-1 { color:var(--accent); }
.order .preview { display:block; margin:6px 0 0 20px; }
.order .content { margin:12px 0 0 20px; max-width:76ch; }
ol,ul { padding-left:24px; margin:8px 0 16px; }
li { margin:6px 0; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; text-align:left; }
th { color:var(--muted); font-weight:600; }
th,td { border-bottom:1px solid var(--line); padding:12px 10px; vertical-align:top; }
.time { white-space:nowrap; font-variant-numeric:tabular-nums; }
footer { margin-top:32px; padding-top:16px; border-top:1px solid var(--line); color:var(--muted); }
@media(max-width:600px) { main { padding:20px 16px; } .status { gap:12px 20px; } }
"""


def escaped(value) -> str:
    """將所有動態文字轉為安全的 HTML 文字或屬性值。"""
    return html.escape(str(value) if value is not None else "待確認", quote=True)


def context_section(value: dict) -> str:
    """呈現模型已驗證的情境與分流，標示所依據的回報時間。"""
    parts = [f"<p><strong>{label}</strong>：{escaped(value[field])}</p>"
             for field, label in advice.CONTEXT_FIELDS if field in value]
    if parts and value.get("as_of"):
        parts.append(f'<p class="muted">建議依據回報時間：{escaped(value["as_of"])}</p>')
    return "".join(parts)


def work_order_section(order: dict) -> str:
    """呈現優先檢查項目與可展開的工單完整步驟。"""
    actions = order["actions"]
    steps = "".join(f"<li>{escaped(action)}</li>" for action in actions)
    causes = "".join(f"<li>{escaped(cause)}</li>" for cause in order["causes"])
    priority_class = "urgent" if order["priority"] == "緊急" else ""
    status = STATUS_NAMES[order["event_status"]]
    context = context_section(order)
    if order.get("revision", 0):
        context = (
            f'<p class="operator-text"><strong>工單說明（使用者確認修訂）</strong>：'
            f'{escaped(order.get("operator_description", order.get("recommendation", "")))}</p>'
            f'<p class="operator-text"><strong>現場備註（人員回報）</strong>：{escaped(order.get("operator_notes") or "無")}</p>'
            f'<p class="muted">第 {int(order["revision"])} 版 · '
            f'{escaped(order["revised_at"])}；原始告警與量測證據保留。</p>')
    preview = (f'<span class="preview">{escaped(order["situation"])}</span>'
               if order.get("situation") else "")
    if order.get("operator_description"):
        preview = f'<span class="preview">{escaped(order["operator_description"].splitlines()[0])}</span>'
    revision = f" · 修訂第 {int(order['revision'])} 版" if order.get("revision", 0) else ""
    source_label = "原始建議來源" if order.get("revision", 0) else "建議來源"
    return f"""
    <details class="order" id="{escaped(order['id'])}">
      <summary><span class="priority {priority_class}">{escaped(order['priority'])}</span>
        <strong>{escaped(order['problem'])}</strong>
        {preview}
        <span class="preview">優先檢查：{escaped(actions[0])}</span>
        <span class="preview muted">{escaped(order['id'])}{revision}　工單：草稿待確認　告警：{escaped(status)}</span>
      </summary>
      <div class="content">
        {context}
        <p><strong>檢查步驟</strong></p><ol>{steps}</ol>
        <p><strong>可能原因，待現場確認</strong></p><ul>{causes}</ul>
        <p class="muted">首次提醒資料時間：{escaped(order['created_at'])}</p>
        <p class="muted">{source_label}：{escaped(order.get('source', '本機規則模板'))}</p>
        <p>請機台負責人確認後，依工廠流程安排檢查。告警恢復不代表維修完成。</p>
      </div>
    </details>"""


def write_results_page(path: pathlib.Path, summary: dict,
                       events: list, work_orders: list) -> None:
    """儲存可離線開啟的結果摘要與工單，不載入外部資源。

    Args:
        path: HTML 結果頁的儲存位置。
        summary: 本次執行狀態、資料區間與結束時狀態。
        events: 本次事件與告警狀態。
        work_orders: 保持 draft 狀態的維修工單草稿。
    """
    ordered = sorted(work_orders, key=lambda order: (
        order["priority"] != "緊急", order["created_at"]))
    orders = "".join(work_order_section(order) for order in ordered)
    if not orders:
        orders = "<p>本次沒有建立維修工單草稿。</p>"
    order_by_event = {order["event_id"]: order["id"] for order in ordered}
    event_rows = []
    for event in events:
        order_id = order_by_event.get(event["id"])
        link = (f'<a href="#{escaped(order_id)}">{escaped(order_id)}</a>'
                if order_id else "—")
        title = escaped(event["advice"]["title"])
        if not order_id and event["advice"].get("situation"):
            title = (f'<details><summary>{title}</summary>{context_section(event["advice"])}'
                     f'<p class="muted">建議來源：{escaped(event["advice"]["source"])}</p></details>')
        event_rows.append(
            f'<tr><td class="time">{escaped(event["start_time"])}</td>'
            f'<td>{title}</td>'
            f'<td class="risk-{int(event["level"])}">{escaped(monitor.LEVEL_NAMES[event["level"]])}</td>'
            f'<td>{escaped(STATUS_NAMES[event["status"]])}</td><td>{link}</td></tr>')
    event_content = (f"""<div class="table-wrap"><table>
      <thead><tr><th>首次提醒資料時間</th><th>事件摘要</th><th>歷史最高警戒</th>
      <th>告警狀態</th><th>工單草稿</th></tr></thead>
      <tbody>{''.join(event_rows)}</tbody></table></div>""" if events
                     else "<p>本次尚無事件紀錄。</p>")
    last_state = summary.get("last_state")
    if last_state:
        equipment = monitor.LEVEL_NAMES[last_state["equipment_level"]]
        quality = monitor.LEVEL_NAMES[last_state["quality_level"]]
        aoi = "正常" if last_state["label"] == "normal" else "異常"
        state = f"""<h2>最後回報狀態</h2><dl class="status" aria-label="結束時狀態">
          <div><dt>設備綜合評估</dt><dd>{equipment}</dd></div>
          <div><dt>資料品質</dt><dd>{quality}</dd></div>
          <div><dt>AOI設備監測</dt><dd>{aoi}</dd></div></dl>"""
    else:
        state = "<p>尚無可判讀的監測資料，設備狀態待確認。</p>"
    period = (f"{escaped(summary.get('first_timestamp'))} ～ {escaped(summary['last_timestamp'])}"
              if summary.get("first_timestamp") and summary.get("last_timestamp")
              else "尚無有效監測區間")
    completion = ("本頁涵蓋已完成的監測區間。" if summary["status"] == "finished"
                  else "本次未完成全部監測，本頁僅保留已讀取的結果。")
    pending = (f"{len(ordered)} 份工單草稿待確認" if ordered else "本次沒有待確認工單")
    body = f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>設備監測結果與工單</title><style>{CSS}</style></head>
<body><main><header><h1>設備監測結果</h1>
<p><strong>{escaped(RUN_NAMES[summary['status']])}</strong>　{completion}</p>
<p class="muted">資料區間（台北時間）：{period}</p>
<nav aria-label="結果導覽"><a href="#orders">查看待確認工單</a><a href="#events">查看事件紀錄</a></nav>
</header>{state}
<div class="notice"><strong>{pending}</strong>
<p>未解除提醒：{int(summary['active_events'])} 件。工單須經現場確認，告警恢復不代表維修完成。</p></div>
<section id="orders"><h2>待確認工單</h2>{orders}</section>
<section id="events"><h2>事件紀錄</h2>{event_content}</section>
<footer>本頁為本次執行的本機結果摘要，可離線開啟。詳細逐筆紀錄另存於同一資料夾。
建議來源：{escaped(summary.get('advice_source', '本機規則模板'))}。</footer>
</main></body></html>"""
    path.write_text(body, encoding="utf-8")
