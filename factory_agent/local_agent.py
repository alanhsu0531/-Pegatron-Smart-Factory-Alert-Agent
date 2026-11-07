"""讓 Ollama 根據本機證據說明情境與查核分流；失敗時保留規則建議。"""

import copy
import json
import math
import queue
import re
import threading
import time
import urllib.request

from factory_agent import advice
from factory_agent import monitor


ENDPOINT = "http://127.0.0.1:11434"
MODEL = "qwen3:4b-instruct"
TIMEOUT = 30
TOOL_NAMES = ("get_event", "get_history", "get_playbook")
TOOLS = [
    {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": {},
                       "additionalProperties": False},
    }} for name, description in zip(TOOL_NAMES, (
        "取得本次事件的風險、有效量測、資料品質與可引用的證據 ID。",
        "取得截至事件當下最近 12 筆回報，不包含未來資料。",
        "取得此事件允許使用的根因候選與維修檢查步驟。",
    ))
]
SYSTEM = (
    "你是馬達驅動泵浦的地端維修助理。先呼叫 get_event、get_history、"
    "get_playbook 各一次，盡量在同一輪呼叫。工具不需要參數。"
    "依已取得的量測、時間趨勢與資料品質，排序根因，產生簡短繁體中文處置說明。"
    "根因只是待現場確認的方向，不可確診；風險等級由程式決定。"
    "只回傳指定 JSON，不輸出推論過程，不新增原因、維修動作或故障機率。"
    "取得三個工具後，回傳 event_id、cause_order（C1/C2 各一次）、evidence_refs、"
    "action_refs、situation、inspection_reason、next_step。"
    "situation：四十字內整合當次感測器、AOI及趨勢，依 observations，勿把灰區當嚴格超標。"
    "溫度情境需交代壓力與振動是否也異常。"
    "趨勢只描述 observations 已確認的溫度持續上升，不宣稱壓力或振動也在上升。"
    "inspection_reason：二十五字內說明第一步能排查什麼，不只重複狀態；程式會保留第一步原文。"
    "理由以『用來區分』或『避免』開頭，說明要區分的問題，不重複第一步。"
    "next_step 是物件，必填 if_confirmed 與 if_not_confirmed，各四十字內。"
    "分別填入現場確認異常／未確認異常時的具體動作，以『檢查、核對、確認、觀察、通知、查看』開頭。"
    "不要在動作重複寫若或如果，程式會補上查核條件。"
    "每條分流必須遵守 follow_up 的 action_id 與檢查對象，至少提到其中一個 targets。"
    "現場確認設備異常後查實體設備；與回報不符時查量測端，不能倒置。"
    "遇到缺值或無效回報，先區分現場異常與採集問題，不推定機械故障；AOI正常也不能排除感測異常。"
    "資料品質事件兩條分流都先查核對應量測端，程式會在現場也異常的分流補上通知負責人。"
    "依知識卡檢查範圍，只寫核對、確認、檢查、觀察或通知，不寫換零件、調參、停機或拆檢操作。"
    "請用繁體中文短句，不輸出數字、時間、ID、英文分析或未量測的確定事實。"
    "evidence_refs 引用 get_event 的證據 ID；action_refs 引用 action_ids，包含 A1 與兩條分流的 action_id。"
)
CHECK_TARGETS = ("負載", "轉速", "冷卻", "風扇", "散熱", "潤滑", "管路", "閥門", "供壓",
                 "濾網", "軸承", "聯軸器", "基座", "傳動", "訊號線", "接線", "固定", "接頭",
                 "通道", "採集", "影像", "觸發", "連線", "映射", "PLC", "電源", "量測",
                 "馬達", "異音", "鬆動", "壓力表", "操作", "轉換", "錯誤碼", "儀表")


def signature(event: dict) -> tuple:
    """辨識風險、事件類型或受影響感測器是否已改變。"""
    return event["level"], event["kind"], tuple(event.get("sensors", []))


def observed_facts(reading: dict, history: list) -> dict:
    """由程式判定當次作業條件與溫升，避免模型自行推算門檻。

    Args:
        reading: 已完成品質檢查的當筆回報。
        history: 截至當下的原始歷史回報。

    Returns:
        三個感測器的作業條件、AOI 狀態、五筆溫升判定與可引用摘要。
    """
    conditions = {}
    for index, sensor in enumerate(monitor.SENSORS):
        value = reading["raw"][sensor]
        if not reading["raw_valid"][sensor]:
            conditions[sensor] = "回報不可用"
        elif value < monitor.STRICT_LOW[index] or value > monitor.STRICT_HIGH[index]:
            conditions[sensor] = "嚴格超標"
        elif value < monitor.NORMAL_LOW[index] or value > monitor.NORMAL_HIGH[index]:
            conditions[sensor] = "灰區"
        else:
            conditions[sensor] = "正常範圍"
    values = [record["temp"] for record in history[-5:]]
    rising = (len(values) == 5 and all(value is not None and math.isfinite(value)
                                     and value != 999 for value in values)
              and all(a <= b for a, b in zip(values, values[1:]))
              and values[-1] - values[0] >= 0.4)
    aoi = "正常" if reading["label"] == "normal" else "異常"
    summary = "、".join(f"{advice.SENSOR_NAMES[sensor]}{condition}"
                       for sensor, condition in conditions.items()) + f"；AOI回報{aoi}。"
    if rising:
        summary += "溫度近期持續上升。"
    return {"sensor_conditions": conditions, "temp_rising": rising,
            "aoi": aoi, "summary": summary}


def context_snapshot(event: dict, reading: dict, history) -> dict:
    """凍結事件當下的工具資料，拒絕未來回報。

    Args:
        event: 程式已判定的事件。
        reading: 當前已完成判讀的回報。
        history: 僅包含已讀資料的近期窗口。

    Returns:
        三個唯讀工具的獨立 JSON 資料，不包含生成事件答案。

    Raises:
        ValueError: 歷史包含當前時點之後的資料。
    """
    recent = []
    history = list(history)[-12:]
    for record in history:
        timestamp = record["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        if record["row"] > reading["row"] or timestamp > reading["timestamp"]:
            raise ValueError("Agent 不可取得未來資料。")
        recent.append({"timestamp": timestamp, "label": record["label"],
                       **{sensor: record[sensor] for sensor in monitor.SENSORS}})
    card = advice.make_advice(event["kind"], event.get("sensors", []))
    evidence = [{"id": f"F{i}", "text": reason}
                for i, reason in enumerate(event["evidence"], 1)]
    facts = observed_facts(reading, history)
    evidence.append({"id": f"F{len(evidence) + 1}", "text": facts["summary"]})
    branch_steps = ((1, 2) if event["kind"] == "aoi" else
                    (0, 1) if event["kind"] in ("missing", "invalid") else
                    (2, 1) if event["kind"] == "model" else (1, 0))
    follow_up = {}
    for branch, index in zip(("if_confirmed", "if_not_confirmed"), branch_steps):
        targets = CHECK_TARGETS
        if branch == "if_not_confirmed" and event["kind"] not in ("aoi", "model", "missing", "invalid"):
            targets = ("感測器", "儀表", "壓力表", "量測", "接線", "固定", "接頭", "讀值", "回報")
        follow_up[branch] = {
            "action_id": f"A{index + 1}",
            "targets": [word for word in targets if word in card["actions"][index]],
        }
    return copy.deepcopy({
        "get_event": {
            "event_id": event["id"], "kind": event["kind"],
            "risk": monitor.LEVEL_NAMES[event["level"]],
            "start_time": event["start_time"], "as_of": reading["timestamp"],
            "evidence_row": event.get("evidence_row", reading["row"]),
            "evidence_time": event.get("evidence_time", reading["timestamp"]),
            "evidence": evidence,
            "raw": reading["raw"], "raw_valid": reading["raw_valid"],
            "imputed": reading["imputed"], "quality": reading["quality"],
            "aoi": reading["label"],
            "observations": facts,
        },
        "get_history": recent,
        "get_playbook": {
            "title": card["title"],
            "causes": {f"C{i}": cause for i, cause in enumerate(card["causes"], 1)},
            "actions": card["actions"],
            "action_ids": [f"A{i}" for i in range(1, len(card["actions"]) + 1)],
            "follow_up": follow_up,
        },
    })


def output_schema(context: dict) -> dict:
    """限制模型只能引用本次事件與已知證據、根因候選。"""
    event = context["get_event"]
    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "event_id": {"type": "string", "enum": [event["event_id"]]},
            "cause_order": {"type": "array", "minItems": 2, "maxItems": 2,
                            "items": {"type": "string", "enum": ["C1", "C2"]}},
            "evidence_refs": {
                "type": "array", "minItems": 1, "maxItems": 6,
                "items": {"type": "string", "enum": [item["id"]
                          for item in event["evidence"]]},
            },
            "action_refs": {"type": "array", "minItems": 1, "maxItems": 3,
                            "items": {"type": "string", "enum": ["A1", "A2", "A3"]}},
            "situation": {"type": "string", "minLength": 8, "maxLength": 60},
            "inspection_reason": {"type": "string", "minLength": 8, "maxLength": 60},
            "next_step": {
                "type": "object", "additionalProperties": False,
                "properties": {branch: {"type": "string", "minLength": 8, "maxLength": 50}
                               for branch in ("if_confirmed", "if_not_confirmed")},
                "required": ["if_confirmed", "if_not_confirmed"],
            },
        },
        "required": ["event_id", "cause_order", "evidence_refs", "action_refs",
                     "situation", "inspection_reason", "next_step"],
    }


def validate_explanation(result: dict, context: dict) -> None:
    """檢查長度、查核分流及明顯矛盾，不能證明自然語言診斷正確。

    Args:
        result: 模型的結構化情境說明。
        context: 當次觀測事實及允許的知識卡範圍。

    Raises:
        ValueError: 文字、查核動作或觀測描述不符合驗證條件。
    """
    branches = result["next_step"]
    if not isinstance(branches, dict) or set(branches) != {"if_confirmed", "if_not_confirmed"}:
        raise ValueError("下一步須有兩條現場查核分流。")
    values = [(result["situation"], 60), (result["inspection_reason"], 60)]
    values.extend((value, 50) for value in branches.values())
    for value, limit in values:
        if (not isinstance(value, str) or not 8 <= len(value) <= limit
                or re.search(r"[\d\[\]<>\x00-\x1f]", value)
                or not re.search(r"[\u4e00-\u9fff]", value)):
            raise ValueError("情境說明須為簡短中文，不能包含數字、標記或控制字元。")
    text = "".join(value for value, _ in values)
    if re.search(r"確診|已確認故障|故障機率|保證|繼續運轉|無須檢查|不需檢查|已排除|"
                 r"更換|拆卸|拆除|拆檢|緊固|對心|短接|切斷|重啟|重置|復歸|調高|調低|"
                 r"設定為|設定成|加壓|卸壓|停機|清理|旁路|"
                 r"(?:設備|機台|泵浦)(?:目前|已|為)?正常", text):
        raise ValueError("情境說明包含未允許的診斷或操作。")
    if any(not re.match(r"(?:請|先|再)?(?:檢查|核對|確認|觀察|通知|查看|查核|查驗|比對)", value)
           for value in branches.values()):
        raise ValueError("每條查核分流都必須有明確動作。")
    if not re.match(r"(?:用來區分|有助區分|避免|用以|以便|釐清)", result["inspection_reason"]):
        raise ValueError("檢查理由須說明目的，不只重複檢查步驟。")
    for branch, rule in context["get_playbook"]["follow_up"].items():
        if (rule["action_id"] not in result["action_refs"]
                or not any(target in branches[branch] for target in rule["targets"])):
            raise ValueError("查核分流與知識卡的檢查範圍不符。")
    facts = context["get_event"]["observations"]
    situation = result["situation"]
    for match in re.finditer(r"(?:壓力|振動)([^。；]{0,18})(?:上升|升高)", situation):
        if "溫度" not in match.group(1):
            raise ValueError("摘要宣稱了未確認的壓力或振動上升趨勢。")
    opposite = "異常" if facts["aoi"] == "正常" else "正常"
    if re.search(r"AOI(?:回報|目前|仍|為)*" + opposite, situation):
        raise ValueError("摘要與 AOI 回報矛盾。")
    strict_count = sum(state == "嚴格超標" for state in facts["sensor_conditions"].values())
    if strict_count < 2 and re.search(r"(?:多項|三項|全部|所有|三個).{0,10}超標", situation):
        raise ValueError("摘要宣稱了不存在的聯合超標。")
    for sensor, name in {**advice.SENSOR_NAMES, "aoi": "AOI"}.items():
        state = facts["aoi"] if sensor == "aoi" else facts["sensor_conditions"][sensor]
        if (state not in ("正常", "正常範圍")
                and re.search(name + r"(?:回報|目前|仍|為|皆|均|值|在|範圍內)*正常", situation)):
            raise ValueError("摘要將異常或不可用資料描述為正常。")
        if (state != "嚴格超標"
                and re.search(name + r"(?:回報|目前|已|值|為)*(?:超標|嚴格超標)", situation)):
            raise ValueError("摘要宣稱了不存在的嚴格超標。")
    if (not facts["temp_rising"]
            and re.search(r"溫度(?:近期|正|呈現|仍|逐步)*(?:持續上升|持續升高|逐步升高)", situation)):
        raise ValueError("摘要宣稱了未確認的持續溫升。")


def validate_result(result: dict, context: dict) -> dict:
    """驗證模型引用後轉回既有建議格式，保留完整的安全檢查步驟。

    Args:
        result: 模型回傳的 JSON 物件。
        context: 本次工具資料，包含允許使用的證據與維修知識卡。

    Returns:
        可供終端、事件紀錄與工單共用的建議。

    Raises:
        ValueError: 事件、欄位、候選排序或證據引用不符。
    """
    event, card = context["get_event"], context["get_playbook"]
    if not isinstance(result, dict) or set(result) != {
            "event_id", "cause_order", "evidence_refs", "action_refs",
            "situation", "inspection_reason", "next_step"}:
        raise ValueError("模型回覆欄位不符。")
    causes, refs = result["cause_order"], result["evidence_refs"]
    evidence = {item["id"]: item["text"] for item in event["evidence"]}
    if (result["event_id"] != event["event_id"]
            or not isinstance(causes, list) or len(causes) != 2
            or any(not isinstance(cause, str) for cause in causes)
            or set(causes) != {"C1", "C2"}
            or not isinstance(refs, list) or not 1 <= len(refs) <= 6
            or any(not isinstance(ref, str) or ref not in evidence for ref in refs)
            or len(set(refs)) != len(refs)):
        raise ValueError("模型的事件或證據引用不符。")
    actions = result["action_refs"]
    if (not isinstance(actions, list) or not 1 <= len(actions) <= 3
            or any(not isinstance(key, str) or key not in card["action_ids"] for key in actions)
            or len(set(actions)) != len(actions) or "A1" not in actions):
        raise ValueError("檢查步驟引用不符。")
    validate_explanation(result, context)
    explanation = {field: result[field].strip().rstrip("。；， ") + "。"
                   for field in ("situation", "inspection_reason")}
    explanation["inspection_reason"] = card["actions"][0] + explanation["inspection_reason"]
    if event["kind"] in ("missing", "invalid"):
        conditions = ("若現場量測也有異常", "若現場量測正常")
    elif event["kind"] == "aoi":
        conditions = ("若影像或現場確認異常", "若未確認異常")
    else:
        conditions = ("若現場確認相同異常", "若現場與回報不同")
    branches = result["next_step"]
    confirmed = branches["if_confirmed"].strip("。； ")
    if event["kind"] in ("missing", "invalid") and "通知" not in confirmed:
        confirmed = "通知現場負責人，並" + confirmed
    explanation["next_step"] = (f"{conditions[0]}，{confirmed}；"
                                f"{conditions[1]}，{branches['if_not_confirmed'].strip('。； ')}。")
    return {
        "title": card["title"], "causes": [card["causes"][key] for key in causes],
        "actions": list(card["actions"]),
        "recommendation": "\n".join(f"{label}：{explanation[field]}"
                                    for field, label in advice.CONTEXT_FIELDS),
        "source": "Ollama 地端模型", "model": MODEL,
        "as_of": event["as_of"], "evidence_refs": refs,
        "basis": [evidence[ref] for ref in refs],
        "action_refs": actions, **explanation,
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止將工業資料重新導向其他端點。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaClient:
    """只連線固定 loopback，使用已下載模型，不代理、不拉取模型。"""

    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        self.checked = False

    def request(self, path: str, payload, deadline: float) -> dict:
        """在剩餘時間內呼叫固定本機 API，限制回覆大小。"""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("地端模型逾時。")
        data = (json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
                if payload is not None else None)
        request = urllib.request.Request(ENDPOINT + path, data=data,
                                         headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=remaining) as response:
            body = response.read(1024 * 1024 + 1)
        if time.monotonic() > deadline:
            raise TimeoutError("地端模型逾時。")
        if len(body) > 1024 * 1024:
            raise ValueError("模型回覆過長。")
        result = json.loads(body)
        if not isinstance(result, dict) or result.get("error"):
            raise ValueError("Ollama 回覆錯誤。")
        return result

    def check_local(self, deadline: float) -> None:
        """確認指定模型有本機權重及工具能力，不限制雲端功能開關。"""
        if self.checked:
            return
        models = self.request("/api/tags", None, deadline).get("models", [])
        if not any(item.get("name") == MODEL and item.get("size", 0) > 0
                   and not item.get("remote_model") for item in models):
            raise ValueError("指定的本機模型尚未就緒。")
        model = self.request("/api/show", {"model": MODEL}, deadline)
        if (model.get("remote_model") or model.get("remote_host")
                or "tools" not in model.get("capabilities", [])):
            raise ValueError("模型不是可用的本機工具模型。")
        self.checked = True

    def explain(self, context: dict, deadline: float) -> dict:
        """最多三輪請求、四次唯讀工具呼叫，輸出驗證後的建議與稽核紀錄。"""
        trace = {"event_id": context["get_event"]["event_id"],
                 "as_of": context["get_event"]["as_of"], "model": MODEL,
                 "requests": 0, "tools": []}
        started = time.monotonic()
        try:
            self.check_local(min(deadline, time.monotonic() + 3))
            trace.update(endpoint=ENDPOINT, local_only=True)
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": "請查詢三個本機工具，整理此次維修檢查方向。"}]
            called = set()
            for attempt in range(3):
                payload = {"model": MODEL, "messages": messages, "stream": False,
                           "keep_alive": "10m", "think": False,
                           "options": {"temperature": 0, "num_ctx": 8192,
                                       "num_predict": 768 if called == set(TOOL_NAMES)
                                       else 256, "seed": 42}}
                if called == set(TOOL_NAMES):
                    payload["format"] = output_schema(context)
                else:
                    payload["tools"] = [tool for tool in TOOLS
                                        if tool["function"]["name"] not in called]
                trace["requests"] += 1
                message = self.request("/api/chat", payload, deadline)["message"]
                if not isinstance(message, dict):
                    raise ValueError("模型訊息格式不符。")
                calls = message.get("tool_calls", [])
                messages.append({key: message[key] for key in ("role", "content", "tool_calls")
                                 if key in message})
                if not calls:
                    if called != set(TOOL_NAMES):
                        raise ValueError("模型未查詢完整的本機工具。")
                    try:
                        result = json.loads(message["content"])
                        trace["result"] = result
                        recommendation = validate_result(result, context)
                    except (ValueError, TypeError) as error:
                        trace.setdefault("validation_errors", []).append(str(error))
                        if attempt == 2:
                            raise
                        messages.append({"role": "user", "content": (
                            f"回覆未通過檢查：{error} 請依原工具資料與 follow_up 修正完整 JSON，"
                            "不要重新呼叫工具，不要新增未量測的事實。")})
                        continue
                    trace.update(status="accepted", result=result,
                                 elapsed_seconds=round(time.monotonic() - started, 3))
                    return {"advice": recommendation, "trace": trace}
                for call in calls:
                    function = call["function"]
                    name = function["name"]
                    if (name not in context or function.get("arguments", {}) != {}
                            or len(trace["tools"]) >= 4):
                        raise ValueError("工具名稱、參數或呼叫次數不符。")
                    value = context[name]
                    trace["tools"].append({"name": name, "result": value})
                    called.add(name)
                    messages.append({"role": "tool", "tool_name": name,
                                     "content": json.dumps(value, ensure_ascii=False,
                                                           allow_nan=False)})
            raise ValueError("已達模型請求上限。")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            reason = ("地端模型逾時" if isinstance(error, TimeoutError) else
                      "Ollama 連線不可用" if isinstance(error, OSError) else str(error))
            trace.update(status="fallback", reason=reason,
                         elapsed_seconds=round(time.monotonic() - started, 3))
            return {"advice": None, "trace": trace}


class LocalAgent:
    """單一背景工作者處理解說；主流程立即顯示規則建議並保存結果。"""

    def __init__(self, trace_stream):
        self.client = OllamaClient()
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.trace_stream = trace_stream
        self.pending = {}
        self.sequence = 0
        self.closed = False
        self.seen = {}
        self.latest = {}
        self.accepted = 0
        self.fallbacks = 0
        self.disabled_reason = None
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        while True:
            job = self.jobs.get()
            if job is None or self.closed:
                return
            token, context, deadline = job
            event_id = context["get_event"]["event_id"]
            if self.latest.get(event_id) != token:
                self.results.put((token, {"advice": None, "trace": {
                    "event_id": event_id, "as_of": context["get_event"]["as_of"],
                    "status": "superseded", "reason": "事件已更新，未呼叫模型。",
                    "requests": 0, "tools": [],
                }}))
                continue
            try:
                result = self.client.explain(context, deadline)
            except Exception as error:
                # 背景錯誤不能讓主流程永遠等待；保留錯誤種類供本機查核。
                result = {"advice": None, "trace": {
                    "event_id": context["get_event"]["event_id"],
                    "status": "fallback", "reason": "地端解說無法完成",
                    "error_type": type(error).__name__, "requests": 0, "tools": [],
                }}
            self.results.put((token, result))

    def update(self, tracker: monitor.EventTracker, reading: dict, history) -> None:
        """僅在需處置事件新增／升級／類型改變，或 AOI 初次提醒時送出快照。"""
        self.poll(tracker)
        for event in tracker.active.values():
            version = signature(event)
            if (event["status"] != "active" or self.seen.get(event["id"]) == version
                    or (event["level"] < 2 and event["kind"] != "aoi")):
                continue
            self.seen[event["id"]] = version
            if self.disabled_reason:
                fallback = copy.deepcopy(event["advice"])
                fallback["source"] = "規則備援"
                tracker.set_advice(event["id"], fallback)
                self.fallbacks += 1
                self.trace_stream.write(json.dumps({
                    "event_id": event["id"], "as_of": reading["timestamp"],
                    "status": "fallback", "reason": self.disabled_reason,
                    "requests": 0, "tools": [],
                }, ensure_ascii=False) + "\n")
                continue
            context = context_snapshot(event, reading, history)
            deadline = time.monotonic() + TIMEOUT
            self.sequence += 1
            self.latest[event["id"]] = self.sequence
            self.pending[self.sequence] = (event["id"], version, deadline)
            self.jobs.put((self.sequence, context, deadline))
        self._status(tracker)

    def poll(self, tracker: monitor.EventTracker) -> None:
        """只在主執行緒套用仍符合事件版本的建議，並儲存工具紀錄。"""
        finished = []
        while True:
            try:
                finished.append(self.results.get_nowait())
            except queue.Empty:
                break
        for token, (event_id, _, deadline) in self.pending.items():
            if time.monotonic() >= deadline:
                finished.append((token, {"advice": None, "trace": {
                    "event_id": event_id, "status": "fallback",
                    "reason": "地端模型逾時，未取得完整工具紀錄",
                    "requests": None, "tools": None,
                }}))
        for token, result in finished:
            if token not in self.pending:
                continue
            event_id, version, _ = self.pending.pop(token)
            event = next(item for item in tracker.events if item["id"] == event_id)
            trace = result["trace"]
            if signature(event) != version or self.latest[event_id] != token:
                trace["status"] = "superseded"
            elif result["advice"]:
                tracker.set_advice(event_id, result["advice"])
                self.accepted += 1
            else:
                self.fallbacks += 1
                fallback = copy.deepcopy(event["advice"])
                fallback["source"] = "規則備援"
                tracker.set_advice(event_id, fallback)
                if not self.client.checked:
                    self.disabled_reason = trace["reason"]
            self.trace_stream.write(json.dumps(trace, ensure_ascii=False, allow_nan=False) + "\n")
            self.trace_stream.flush()
        self._status(tracker)

    def _status(self, tracker: monitor.EventTracker) -> None:
        tracker.agent_pending = {event_id for event_id, _, _ in self.pending.values()}
        if self.pending:
            tracker.agent_status = "地端 AI 整理中 · 先用規則建議"
        elif self.disabled_reason:
            tracker.agent_status = "規則備援 · Ollama 未就緒"
        elif self.fallbacks:
            tracker.agent_status = "地端 AI · 部分規則備援"
        elif self.accepted:
            tracker.agent_status = "Ollama 地端模型"
        else:
            tracker.agent_status = "地端 AI 待命"

    def close(self, tracker: monitor.EventTracker, wait: bool = True) -> None:
        """正常結束時完成限時任務；中斷時不等待模型，保留已有建議。"""
        if wait:
            while self.pending:
                self.poll(tracker)
                if self.pending:
                    time.sleep(0.05)
        else:
            for event_id, _, _ in self.pending.values():
                self.trace_stream.write(json.dumps({
                    "event_id": event_id, "status": "cancelled",
                    "reason": "播放已停止，保留已有規則建議。",
                }, ensure_ascii=False) + "\n")
            self.trace_stream.flush()
            self.pending.clear()
            tracker.agent_pending = set()
            tracker.agent_status = "已停止 · 保留已完成建議"
        self.closed = True
        self.jobs.put(None)

    def summary(self) -> dict:
        """回傳可查核的使用統計，不將模型成功等同診斷正確。"""
        return {"enabled": True, "model": MODEL, "accepted": self.accepted,
                "fallbacks": self.fallbacks, "disabled_reason": self.disabled_reason}
