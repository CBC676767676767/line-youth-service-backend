"""Local-only LINE interaction renderer with bounded, transient conversation state.

No network, database, logging, identity collection, or case authorization. Handoffs
carry public menu selections only and are never login or case-access credentials.
LINE limits: https://developers.line.biz/en/docs/messaging-api/using-quick-reply/
"""
import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
from collections import OrderedDict
from datetime import datetime
from threading import RLock
from urllib.parse import urlencode, urlsplit, urlunsplit

from app.precheck_engine import TAIPEI
from app.precheck_schema import PrecheckBundle


STATE_TTL_SECONDS = 1200
PAGE_SIZE = 9
COMMANDS = frozenset({"選單", "預檢", "文件", "進度", "補正", "安全", "協助"})
STATIC_POSTBACKS = {
    "youth:start": "預檢", "youth:rules": "_rules", "youth:documents": "文件",
    "youth:tracking": "進度", "youth:safety": "安全", "youth:menu": "選單",
    "youth:correction": "補正", "youth:help": "協助", "youth:result": "_result",
}
TOKEN_RE = re.compile(r"youth:v2\.([A-Za-z0-9_-]{1,240})\.([A-Za-z0-9_-]{43})\Z")
HANDOFF_RE = re.compile(r"[A-Za-z0-9_-]{32}\Z")
PURCHASE = ("尚未購買", "已經購買")
BILLING = ("月費訂閱", "年費訂閱", "單獨額度／API", "訂閱＋額外額度")
CHANNEL = ("軟體官方網站", "代購", "集合平台", "App Store／Play", "不確定通路")
PRIVACY = "聊天室不收姓名、生日、證件、帳號、卡號、驗證碼或附件。完整填答、文件問題及案件內容請登入網頁查看。"


def _time(now=None):
    value = now or datetime.now(TAIPEI)
    return int((value.replace(tzinfo=TAIPEI) if value.tzinfo is None else value).timestamp())


def _short(value, limit):
    raw = str(value).encode("utf-16-le")
    return str(value) if len(raw) <= limit * 2 else raw[:(limit - 1) * 2].decode("utf-16-le", errors="ignore") + "…"


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _version(bundle):
    raw = json.dumps(bundle.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _unpack(command):
    if not isinstance(command, str) or len(command) > 300:
        return None
    match = TOKEN_RE.fullmatch(command)
    if not match:
        return None
    try:
        raw = base64.urlsafe_b64decode(match[1] + "=" * (-len(match[1]) % 4))
        state = json.loads(raw)
        if not isinstance(state, list) or len(state) != 6:
            return None
        expiry, version, session, revision, kind, choice = state
        if (type(expiry) is not int or not 0 < expiry < 100_000_000_000
                or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{16}", version)
                or not isinstance(session, str) or not re.fullmatch(r"[0-9a-f]{24}", session)
                or type(revision) is not int or not 0 <= revision <= 1000
                or type(kind) is not int or kind not in {0, 1, 2, 3}
                or type(choice) is not int or not 0 <= choice <= 100
                or _b64(raw) != match[1]
                or json.dumps(state, separators=(",", ":")).encode() != raw):
            return None
        return state, match[1], match[2]
    except (ValueError, TypeError, UnicodeError):
        return None


def normalize_event_command(event: dict) -> str | None:
    """No arbitrary text, media, personal fields or case URLs enter the queue."""
    if not isinstance(event, dict):
        return None
    if event.get("type") == "follow":
        return "選單"
    if event.get("type") == "message":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("type") != "text":
            return None
        value = message.get("text")
        if isinstance(value, str) and len(value) <= 20 and value.strip() in COMMANDS:
            return value.strip()
    if event.get("type") == "postback":
        postback = event.get("postback")
        value = postback.get("data") if isinstance(postback, dict) else None
        if isinstance(value, str):
            if value in STATIC_POSTBACKS:
                return STATIC_POSTBACKS[value]
            if value.startswith("youth:"):
                return value if _unpack(value) else "youth:invalid"
    return None


def is_precheck_command(command) -> bool:
    """Classify renderer failure scope without treating safety errors as precheck failures."""
    if not isinstance(command, str):
        return False
    command = STATIC_POSTBACKS.get(command, command)
    if command in {"預檢", "_result"}:
        return True
    state = _unpack(command)
    return bool(state and state[0][4] in {0, 1})


class ConversationStore:
    """Bounded process-local memory only; lazy TTL cleanup and explicit shutdown wipe."""

    def __init__(self, max_sessions=1000, max_handoffs=1000, max_events=2000):
        if any(type(value) is not int or value < 1 for value in (max_sessions, max_handoffs, max_events)):
            raise ValueError("Conversation store bounds must be positive integers")
        self.max_sessions, self.max_handoffs, self.max_events = max_sessions, max_handoffs, max_events
        self.sessions, self.handoffs, self.events = OrderedDict(), OrderedDict(), OrderedDict()
        self.safety_sessions = OrderedDict()
        self.lock = RLock()

    def clear(self):
        with self.lock:
            self.sessions.clear()
            self.safety_sessions.clear()
            self.handoffs.clear()
            self.events.clear()

    def _purge(self, timestamp):
        for bucket in (self.sessions, self.safety_sessions, self.handoffs, self.events):
            for key in [key for key, value in bucket.items() if value["expires"] <= timestamp]:
                del bucket[key]

    @staticmethod
    def _put(bucket, key, value, maximum):
        bucket[key] = value
        bucket.move_to_end(key)
        while len(bucket) > maximum:
            bucket.popitem(last=False)

    def _new(self, actor, bundle, timestamp, mode):
        session = {"sid": secrets.token_hex(12), "revision": 0, "expires": timestamp + STATE_TTL_SECONDS,
                   "version": _version(bundle), "mode": mode, "answers": [], "page": 0,
                   "status": "active", "safe_result": None}
        bucket = self.safety_sessions if mode == "safety" else self.sessions
        if mode != "safety":
            self._forget_result_views(actor)
        self._put(bucket, actor, session, self.max_sessions)
        return session

    def _forget_result_views(self, actor):
        # A retried result-view event must not resurrect a superseded success card.
        for key in [key for key, value in self.events.items() if key[0] == actor and value.get("result_view")]:
            del self.events[key]


_DEFAULT_STORE = ConversationStore()


def _store(store):
    return _DEFAULT_STORE if store is None else store


def _signature(payload, actor_key, secret_key):
    key = secret_key.encode() if isinstance(secret_key, str) else secret_key
    body = json.dumps(["youth-chat-v2", actor_key, payload], separators=(",", ":")).encode()
    return _b64(hmac.new(key, body, hashlib.sha256).digest())


def _token(session, kind, choice, actor, secret):
    data = [session["expires"], session["version"], session["sid"], session["revision"], kind, choice]
    payload = _b64(json.dumps(data, separators=(",", ":")).encode())
    return "youth:v2." + payload + "." + _signature(payload, actor, secret)


def _safe_url(value):
    if not isinstance(value, str) or len(value) > 1900 or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        if ((parsed.scheme == "https" or local) and parsed.hostname and "\\" not in value
                and parsed.username is None and parsed.password is None):
            _ = parsed.port
            return value
    except ValueError:
        pass
    return None


def _handoff_url(public_url, token):
    if not _safe_url(public_url):
        return None
    parsed = urlsplit(public_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode({"line_handoff": token}), ""))


def login_entry_url(public_url, action="tracking"):
    """Use only the configured origin/path; do not propagate case IDs or caller queries."""
    if not _safe_url(public_url) or action not in {"tracking", "supplement", "apply", "safety"}:
        return None
    parsed = urlsplit(public_url)
    path = parsed.path or "/"
    if parsed.hostname != "liff.line.me" and path.rstrip("/") == "/precheck":
        path = "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode({"entry": "line", "action": action}), ""))


def _saved_result_url(public_url, token):
    """The opaque hint identifies a lookup; the result endpoint still requires case authorization."""
    if not isinstance(token, str) or not HANDOFF_RE.fullmatch(token):
        return None
    url = login_entry_url(public_url, "tracking")
    if not url:
        return None
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                       urlencode({"entry": "line", "action": "tracking", "line_result": token}), ""))


def _action(label, command=None):
    return {"type": "action", "action": {"type": "message", "label": label, "text": command or label}}


def _uri(label, url):
    return {"type": "action", "action": {"type": "uri", "label": label, "uri": url}}


def _postback(label, session, kind, choice, actor, secret):
    return {"type": "action", "action": {
        "type": "postback", "label": _short(label, 20), "displayText": _short(label, 200),
        "data": _token(session, kind, choice, actor, secret),
    }}


def _text(content, items=None):
    message = {"type": "text", "text": content}
    if items:
        message["quickReply"] = {"items": items}
    return message


def _menu(public_url=None):
    items = [_action(label) for label in ("預檢", "文件", "進度", "補正", "安全", "協助")]
    url = login_entry_url(public_url)
    if url:
        items.append(_uri("登入網頁查看", url))
    return items


def _flex(title, body, *, color="#245B78", buttons=None, alt=None):
    contents = {
        "type": "bubble",
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": [
            {"type": "text", "text": title, "weight": "bold", "size": "lg", "color": color, "wrap": True},
            {"type": "text", "text": body, "wrap": True, "size": "sm", "color": "#334155"},
        ]},
    }
    if buttons:
        contents["footer"] = {"type": "box", "layout": "vertical", "spacing": "sm", "contents": [
            {"type": "button", "style": "secondary", "action": action} for action in buttons
        ]}
    return {"type": "flex", "altText": _short(alt or title, 400), "contents": contents}


def _source(bundle, now):
    current = datetime.fromtimestamp(_time(now), TAIPEI).date()
    notice = "公開來源整理，未經機關核定。" if bundle.snapshot else "演示／待確認規則。"
    if not bundle.rules.valid_from <= current <= bundle.rules.valid_until:
        notice += "此版本目前不在有效期間，請確認最新公告。"
    text = notice + "\n規則版本：" + bundle.rules.version + "\n來源：" + bundle.rules.source.title
    if _safe_url(bundle.rules.source.url):
        text += "\n" + bundle.rules.source.url
    return text


def failure_reply():
    return [_text("本次聊天操作暫時無法完成，未代為送件。請稍後重試，或輸入「選單」返回服務。", [_action("選單")])]


def _invalid_reply(kind="expired"):
    text = {"stale": "這是上一個步驟的舊按鈕，已忽略這次點選；目前填答沒有被改寫。請輸入「預檢」重新開始。",
            "expired": "這組按鈕已過期或無法驗證，請輸入「預檢」重新開始。",
            "changed": "規則版本已更新，舊按鈕已停止使用。請輸入「預檢」依最新版本重新開始。"}[kind]
    return [_text(text, [_action("預檢"), _action("選單")])]


def _rules_reply(bundle, now):
    """Public configuration only; never render the current actor's answers or amounts."""
    params = bundle.rules.params
    title = "公開補助規則摘要" if bundle.snapshot else (
        "DEMO／合成規則摘要" if bundle.rules.status == "demo" else "待確認規則摘要")
    lines = [
        "以下為規則資料摘要，不是個人適用判定，也不代表正式送件或核定。",
        "設籍條件：新竹市；自填仍須核對文件。",
        f"出生日期區間：{params.birth_date_from} 至 {params.birth_date_until}（含首尾）。",
        f"購買日期區間：{params.purchase_date_from} 至 {params.purchase_date_until}（含首尾）。",
    ]
    if params.acceptance_date_from and params.acceptance_date_until:
        lines.append(f"公告受理期間：{params.acceptance_date_from} 至 {params.acceptance_date_until}；"
                     "即時經費未知，不保留期限或額度。")
    channel_labels = {"official": "官方網站", "marketplace": "集合平台", "agent": "代購",
                      "app_store": "App 商店", "other": "其他通路", "unsure": "不明通路"}
    if params.restricted_channels:
        lines.append("規則列出的通路限制：" + "、".join(channel_labels[value] for value in params.restricted_channels) + "。")
    if params.restrict_standalone_credits:
        lines.append("單獨儲值／額度受限制；訂閱內含額度須分開判斷。")
    lines.extend(["工具列名不保證所有方案或交易適用；未知方案、通路及公告歧義須確認。", _source(bundle, now)])
    buttons = [{"type": "message", "label": "開始預檢", "text": "預檢"},
               {"type": "message", "label": "返回選單", "text": "選單"}]
    if _safe_url(bundle.rules.source.url):
        buttons.insert(0, {"type": "uri", "label": "查看官方公告" if bundle.rules.source.kind == "official" else "查看規則來源",
                           "uri": bundle.rules.source.url})
    return [_flex(title, "\n".join(lines), buttons=buttons)]


def _step(session, actor, bundle, secret, public_url=None):
    step, answers = len(session["answers"]), session["answers"]
    if step == 1:
        page = session["page"]
        start = page * PAGE_SIZE
        tools = bundle.tools[start:start + PAGE_SIZE]
        items = [_postback(tool.name, session, 0, start + index, actor, secret) for index, tool in enumerate(tools)]
        items.append(_postback("其他／未列名工具", session, 0, len(bundle.tools), actor, secret))
        if page:
            items.append(_postback("上一頁工具", session, 1, page - 1, actor, secret))
        if start + PAGE_SIZE < len(bundle.tools):
            items.append(_postback("下一頁工具", session, 1, page + 1, actor, secret))
        text = f"第 2／4 步：選擇 AI 工具（第 {page + 1} 頁）。\n列名不代表獲准；查詢完整方案請進入網頁。"
    else:
        labels, text = {
            0: (PURCHASE, "第 1／4 步：目前是否已購買？\n先選工具與交易情境，接著到網頁完成預檢。"),
            2: (BILLING, "第 3／4 步：選擇計費方式。\n訂閱內含額度與額外購買額度請分開判斷。"),
            3: (CHANNEL, "第 4／4 步：選擇實際或預計購買通路。\n這些選項尚未查驗交易資料。"),
        }[step]
        items = [_postback(label, session, 0, index, actor, secret) for index, label in enumerate(labels)]
    items.append(_action("選單"))
    if not answers:
        text += "\n" + PRIVACY + "\n按鈕自開始起 20 分鐘有效，舊步驟按鈕不能改寫新步驟。"
    # Every mobile action has the identical signed action in a desktop Flex card.
    # Split long tool pages into short cards while staying below five reply messages.
    desktop_actions = [copy.deepcopy(item["action"]) for item in items]
    if _safe_url(public_url):
        parsed = urlsplit(public_url)
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        desktop_actions.append({"type": "uri", "label": "改用網頁預檢", "uri": url})
    messages = [_text(text, items)]
    for start in range(0, len(desktop_actions), 6):
        messages.append(_flex(f"第 {step + 1}／4 步：點選選項", "電腦與手機皆可點選。改用網頁時請重新填答，聊天選項不會帶入。",
                              buttons=desktop_actions[start:start + 6]))
    return messages


def _choices(session, bundle):
    stage, tool, billing, channel = session["answers"]
    return {"purchase_stage": ("planning", "purchased")[stage],
            "tool_id": bundle.tools[tool].id if tool < len(bundle.tools) else None,
            "billing_type": ("monthly", "annual", "credits", "other")[billing],
            "billing_component": ("subscription", "subscription", "standalone_credits", "mixed")[billing],
            "purchase_channel": ("official", "agent", "marketplace", "app_store", "unsure")[channel]}


def _handoff(session, actor, bundle, store, public_url, now):
    session["status"] = "awaiting_web"
    token = secrets.token_urlsafe(24)
    store._put(store.handoffs, token, {"expires": session["expires"], "actor": actor,
               "sid": session["sid"], "choices": _choices(session, bundle)}, store.max_handoffs)
    session["handoff"] = token
    url = _handoff_url(public_url, token)
    body = "已完成 4 項工具與交易選擇。完整預檢尚未完成，不能判定資格、期限或金額。\n請到網頁補充完整條件；此連結不代表登入，也不代表已送件。"
    if not url:
        body += "\n網頁入口尚未設定，請由正式公告入口洽詢。"
    buttons = [{"type": "uri", "label": "開啟完整網頁預檢", "uri": url}] if url else []
    buttons.append({"type": "postback", "label": "查看預檢摘要", "data": "youth:result"})
    return [_flex("待完成網頁預檢", body, buttons=buttons),
            _text(_source(bundle, now), _menu(public_url))]


def _current_handoff(token, store, timestamp):
    if not isinstance(token, str) or not HANDOFF_RE.fullmatch(token):
        return None
    store._purge(timestamp)
    item = store.handoffs.get(token)
    session = store.sessions.get(item["actor"]) if item else None
    if not item or not session or session["sid"] != item["sid"]:
        return None
    return item, session


def read_handoff(token, *, store=None, now=None):
    """Return only non-sensitive prefill choices. This grants no login/case access."""
    target = _store(store)
    with target.lock:
        current = _current_handoff(token, target, _time(now))
        return copy.deepcopy(current[0]["choices"]) if current else None


def consume_handoff(token, *, store=None, now=None):
    target = _store(store)
    with target.lock:
        current = _current_handoff(token, target, _time(now))
        if not current:
            return None
        del target.handoffs[token]
        return copy.deepcopy(current[0]["choices"])


def _aggregate(result):
    """Accept only backend result counts; never retain checks, inputs or documents."""
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
        return None
    summary = result["summary"]
    keys = ("required_total", "completed", "incomplete", "issues")
    if any(type(summary.get(key)) is not int or not 0 <= summary[key] <= 1000 for key in keys):
        return None
    if summary["completed"] + summary["incomplete"] != summary["required_total"] or summary["issues"] > summary["required_total"]:
        return None
    checks = result.get("checks", [])
    action = any(isinstance(check, dict) and check.get("required") and check.get("outcome") == "action_needed"
                 for check in checks) if isinstance(checks, list) else False
    outcome = ("incomplete" if summary["incomplete"] else "action_needed" if action else
               "manual_review" if summary["issues"] else "no_issue")
    version = result.get("rules_version", "")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", version):
        version = "unknown"
    mode = result.get("mode")
    return {"summary": {key: summary[key] for key in keys}, "rules_version": version,
            "mode": mode if isinstance(mode, str) and mode in {"public_advisory", "demo", "draft", "confirmed"} else "unknown",
            "outcome": outcome}


def record_handoff_result(token, full_backend_result, *, saved=False, store=None, now=None):
    """Only an authenticated, committed save may set saved=True; input result flags are ignored."""
    aggregate = _aggregate(full_backend_result)
    if aggregate is None or type(saved) is not bool:
        return False
    aggregate["saved"] = saved
    target = _store(store)
    with target.lock:
        current = _current_handoff(token, target, _time(now))
        if not current:
            return False
        _, session = current
        session["safe_result"] = aggregate
        session["status"] = aggregate["outcome"]
        session["revision"] += 1
        target._forget_result_views(current[0]["actor"])
        return True


def _mark_failure(session, actor, target):
    session["safe_result"] = {"outcome": "failed"}
    session["status"] = "failed"
    session["revision"] += 1
    target._forget_result_views(actor)


def record_handoff_failure(token, *, store=None, now=None):
    """Replace an earlier aggregate with a generic failure marker; retain no error data."""
    target = _store(store)
    with target.lock:
        current = _current_handoff(token, target, _time(now))
        if not current:
            return False
        item, session = current
        _mark_failure(session, item["actor"], target)
        return True


def record_actor_failure(actor_key, *, store=None, now=None):
    """Call for failed precheck/result operations, never for a safety-practice error."""
    if not isinstance(actor_key, str) or not actor_key:
        return False
    target = _store(store)
    with target.lock:
        target._purge(_time(now))
        session = target.sessions.get(actor_key)
        if not session:
            return False
        _mark_failure(session, actor_key, target)
        return True


def conversation_state(actor_key, *, store=None, now=None):
    """Safe simulator state: no answers, identity, handoff token, documents or case IDs."""
    target = _store(store)
    with target.lock:
        target._purge(_time(now))
        practice = target.safety_sessions.get(actor_key)
        current = target.sessions.get(actor_key) or practice
        if not current:
            return {"status": "none", "revision": None, "step": 0}
        state = {"status": current["status"], "revision": current["revision"],
                "step": len(current["answers"]), "mode": current["mode"],
                "expires_at": current["expires"], "result": copy.deepcopy(current["safe_result"])}
        if practice:
            state["practice"] = {"status": practice["status"], "revision": practice["revision"],
                                 "expires_at": practice["expires"]}
        return state


def result_reply(safe_result, *, public_url=None, handoff_token=None):
    """Three visual variants; manual review and incomplete remain distinct statuses."""
    result = safe_result if isinstance(safe_result, dict) else None
    outcome = result.get("outcome") if result else "incomplete"
    if outcome == "failed":
        return [_flex("本次預檢未完成", "本次預檢發生問題，先前摘要已停止顯示，不能視為目前沒有異常。"
                      "請重新進入網頁預檢；本次聊天操作未代為送件。",
                      color="#A25C12", buttons=[{"type": "message", "label": "重新預檢", "text": "預檢"},
                                                {"type": "message", "label": "返回選單", "text": "選單"}])]
    title, color = {
        "no_issue": ("已執行檢查未發現異常", "#176B50"),
        "action_needed": ("有待處理事項", "#A25C12"),
        "manual_review": ("需要人工確認", "#245B78"),
        "incomplete": ("預檢尚未完成", "#245B78"),
    }.get(outcome, ("預檢尚未完成", "#245B78"))
    saved = bool(result and result.get("saved") is True)
    body = ("已保存預檢（未正式送件）。請登入網頁查看本次結果與待辦。" if saved else
            "此預檢尚未保存。請回到原預檢頁登入後保存；另開網頁時需重新填答。")
    body += "聊天摘要不代表核定、正式送件或補正已受理。"
    if result:
        counts = result["summary"]
        body = (f"已完成 {counts['completed']} 項，未完成 {counts['incomplete']} 項，"
                f"待處理／確認 {counts['issues']} 項。\n規則版本：{result['rules_version']}\n" + body)
        if result.get("mode") == "demo":
            body = "DEMO／合成規則範例，非目前申請結果。\n" + body
    if saved:
        url = _saved_result_url(public_url, handoff_token)
        label = "登入查看本次結果與待辦"
        if not url:
            body += "\n本次結果連結無法使用，請回到原網頁查看已保存紀錄。"
    else:
        if isinstance(handoff_token, str) and HANDOFF_RE.fullmatch(handoff_token):
            url = _handoff_url(public_url, handoff_token)
        elif _safe_url(public_url):
            parsed = urlsplit(public_url)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        else:
            url = None
        label = "開啟完整網頁預檢"
    buttons = [{"type": "uri", "label": label, "uri": url}] if url else []
    buttons.append({"type": "message", "label": "返回選單", "text": "選單"})
    return [_flex(title, body, color=color, buttons=buttons)]


def result_reply_from_backend(result, *, public_url=None):
    """Render backend-derived gallery/results without retaining or exposing full evidence."""
    aggregate = _aggregate(result)
    return failure_reply() if aggregate is None else result_reply(aggregate, public_url=public_url)


def _safety(session, actor, secret):
    if session["status"] == "active":
        return [_text("安全提醒不影響補助適用結果。先選擇這次想練習的情境。", [
            _postback("辨識可疑補助訊息", session, 2, 0, actor, secret),
            _postback("保護個人資料", session, 2, 1, actor, secret), _action("選單")])]
    question = ("收到要求先轉帳才能領補助的訊息，你會怎麼做？" if session.get("purpose") == 0 else
                "有人在聊天中索取帳號與一次性驗證碼，聲稱可代辦補助，你會怎麼做？")
    return [_text(question + "\n此練習不評定補助資格。", [
        _postback("另由官方入口確認", session, 3, 0, actor, secret),
        _postback("直接照訊息要求操作", session, 3, 1, actor, secret), _action("選單")])]


def _apply(command, actor, bundle, secret, now, public_url, target):
    timestamp = _time(now)
    if command.startswith("youth:"):
        unpacked = _unpack(command)
        if not unpacked or not actor or not secret:
            return _invalid_reply()
        data, payload, signature = unpacked
        expiry, version, sid, revision, kind, choice = data
        if (not hmac.compare_digest(signature, _signature(payload, actor, secret))
                or not 0 < expiry - timestamp <= STATE_TTL_SECONDS):
            return _invalid_reply()
        bucket = target.safety_sessions if kind in {2, 3} else target.sessions
        session = bucket.get(actor)
        if version != _version(bundle):
            return _invalid_reply("changed")
        if not session or session["sid"] != sid or session["revision"] != revision:
            return _invalid_reply("stale")
        if session["version"] != version or session["expires"] != expiry:
            return _invalid_reply()
        if session["mode"] == "precheck" and session["status"] == "active":
            step = len(session["answers"])
            if kind == 1 and step == 1 and choice <= max(0, (len(bundle.tools) - 1) // PAGE_SIZE):
                session["page"] = choice
            elif kind == 0 and step < 4 and choice < (2, len(bundle.tools) + 1, 4, 5)[step]:
                session["answers"].append(choice)
                session["page"] = 0
            else:
                return _invalid_reply("stale")
            session["revision"] += 1
            return (_handoff(session, actor, bundle, target, public_url, now) if len(session["answers"]) == 4
                    else _step(session, actor, bundle, secret, public_url))
        if session["mode"] == "safety":
            if kind == 2 and choice in {0, 1} and session["status"] == "active":
                session["purpose"], session["status"] = choice, "quiz"
                session["revision"] += 1
                return _safety(session, actor, secret)
            if kind == 3 and choice in {0, 1} and session["status"] == "quiz":
                session["revision"] += 1
                session["status"] = "completed"
                prefix = "這個做法較安全。" if choice == 0 else "請先停止操作，另由官方入口確認。"
                return [_text(prefix + "\n" + PRIVACY + "\n本練習不影響補助結果，也不代表軟體或交易已通過安全驗證。",
                              _menu(public_url))]
        return _invalid_reply("stale")
    if command == "_result":
        session = target.sessions.get(actor)
        if session and session["version"] != _version(bundle):
            return _invalid_reply("changed")
        return result_reply(session["safe_result"] if session else None, public_url=public_url,
                            handoff_token=session.get("handoff") if session else None)
    if command in {"預檢", "安全"}:
        if not isinstance(actor, str) or not actor or not isinstance(secret, (str, bytes)) or not secret:
            return failure_reply()
        session = target._new(actor, bundle, timestamp, "precheck" if command == "預檢" else "safety")
        return _step(session, actor, bundle, secret, public_url) if command == "預檢" else _safety(session, actor, secret)
    if command in COMMANDS or command == "_rules":
        # Navigating away invalidates displayed choice buttons without deleting an aggregate.
        session = target.sessions.get(actor)
        if session:
            session["revision"] += 1
            if session["status"] == "active":
                session["status"] = "paused"
    if command == "_rules":
        return _rules_reply(bundle, now)
    if command == "選單":
        buttons = [{"type": "message", "label": label, "text": label} for label in ("預檢", "文件", "進度", "補正", "安全", "協助")]
        return [_flex("青年數位工具補助服務", "先用按鈕選擇工具與交易情境，再到網頁完成預檢。\n" + PRIVACY,
                      buttons=buttons)]
    if command in {"文件", "進度", "補正"}:
        titles = {"文件": "登入查看文件清單", "進度": "登入查看申請進度", "補正": "登入查看補正待辦"}
        body = {"文件": "個人備件與文件問題只在登入後的網頁顯示，請勿傳送附件到聊天室。",
                "進度": "聊天室不顯示完整案件，也不會用您貼上的案件網址授權查詢。實際進度以正式平台紀錄為準。",
                "補正": "登入網頁確認待辦與正式通知。聊天點選不代表補正已提交或已受理，沒有正式通知就不建立補件倒數。"}[command]
        url = login_entry_url(public_url, {"文件": "apply", "進度": "tracking", "補正": "supplement"}[command])
        buttons = [{"type": "uri", "label": titles[command], "uri": url}] if url else []
        if not buttons:
            body += "\n登入入口尚未設定；請由正式公告入口查詢。"
        return [_flex(titles[command], body, buttons=buttons), _text(_source(bundle, now), _menu(public_url))]
    if command == "協助":
        return [_text("可輸入：選單、預檢、文件、進度、補正、安全、協助。搜尋完整方案及個人資料請使用網頁。"
                      "\n目前沒有串接即時人工客服；承辦聯絡方式請以官方公告頁面為準。\n" + _source(bundle, now),
                      _menu(public_url))]
    return [_text("無法辨識這次操作，請使用服務按鈕。自由文字、案件連結與附件不會成為預檢填答或案件授權。\n" + PRIVACY,
                  _menu(public_url))]


def build_reply(command: str, actor_key: str, bundle: PrecheckBundle, secret_key: str | bytes,
                now: datetime, public_url: str | None = None, *, store=None, event_key=None) -> list[dict]:
    """Render local replies; serialized revisions make stale buttons harmless.

    A stable webhook event_key returns the cached safe reply without applying a
    transition twice. Only allowlisted commands should reach this function.
    """
    target = _store(store)
    command = STATIC_POSTBACKS.get(command, command) if isinstance(command, str) else "隱私提醒"
    cache_key = (actor_key, event_key) if isinstance(actor_key, str) and isinstance(event_key, str) and 0 < len(event_key) <= 128 else None
    with target.lock:
        timestamp = _time(now)
        target._purge(timestamp)
        cached = target.events.get(cache_key) if cache_key else None
        if cached:
            if cached["version"] != _version(bundle):
                return _invalid_reply("changed")
            return copy.deepcopy(cached["messages"])
        messages = _apply(command, actor_key, bundle, secret_key, now, public_url, target)
        if cache_key:
            target._put(target.events, cache_key, {"expires": timestamp + STATE_TTL_SECONDS,
                        "version": _version(bundle), "result_view": command == "_result",
                        "messages": copy.deepcopy(messages)}, target.max_events)
        return messages

