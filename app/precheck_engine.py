"""Pure, deterministic self-report checks; no HTTP, persistence or document verification."""

import calendar
import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from app.precheck_schema import PrecheckBundle, PrecheckInput


TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_BUNDLE_PATH = Path(__file__).parent / "data" / "precheck-demo.json"
DISCLAIMERS = [
    "本結果僅為申請前預檢，不等於正式送件，不保留期限或補助額度。",
    "填答均為使用者自述；尚未核對文件，不能視為資格或交易已驗證。",
    "LINE 登入僅用於帳號登入，不能當成身分或戶籍查證。",
    "本系統未與政府平台介接，保存草稿不代表官方案件已同步。",
    "行政預檢與資安提醒分開；資安問答不影響補助適用結果。",
]
UNCOVERED = ["收據內容及真偽", "付款人與交易付款一致性", "戶籍及身分真實性", "政府受理與核定狀態", "軟體與交易安全"]


def load_bundle(path=None) -> PrecheckBundle:
    """Validate every setting before use; malformed configuration fails closed at the API boundary."""
    content = Path(path or DEFAULT_BUNDLE_PATH).read_text(encoding="utf-8")
    return PrecheckBundle.model_validate_json(content)


def _rule_block(bundle, demo_allowed, today):
    rules = bundle.rules
    if rules.status == "demo" and not demo_allowed:
        return "demo_disabled", "目前環境不允許使用演示規則，待正式規則確認。"
    if rules.status == "draft" and bundle.snapshot is None:
        return "rules_unconfirmed", "規則仍為草稿，尚未由機關確認。"
    if not rules.valid_from <= today <= rules.valid_until:
        return "rules_out_of_period", "規則尚未生效或已過期，需確認適用版本。"
    return None


def public_catalog(bundle: PrecheckBundle, *, demo_allowed=True) -> dict:
    block = _rule_block(bundle, demo_allowed, datetime.now(TAIPEI).date())
    data = bundle.model_dump(mode="json")
    rules = {k: v for k, v in data["rules"].items() if k not in {"params", "confirmed_by"}}
    disclaimers = list(DISCLAIMERS)
    if bundle.rules.status == "demo":
        disclaimers.insert(0, "DEMO 演示模式：工具、方案與條件均為合成資料，不代表政府公告。")
    if block:
        disclaimers.insert(0, block[1])
    if bundle.snapshot:
        disclaimers.insert(0, _public_source_notice(bundle))
    return {
        "mode": "public_advisory" if bundle.snapshot else bundle.rules.status, "demo": bundle.rules.status == "demo",
        "available": block is None, "rules_version": bundle.rules.version,
        "catalog_version": bundle.catalog_version, "input_version": bundle.input_version,
        "rules": rules, "tools": data["tools"] if demo_allowed or bundle.rules.status != "demo" else [],
        "application_types": data["rules"]["params"]["application_types"],
        "official_application_url": bundle.official_application_url, "disclaimers": disclaimers,
        "snapshot": data["snapshot"], "sources": data["sources"],
        "qualification_categories": data["rules"]["params"]["qualification_categories"],
        "pending_confirmations": data["pending_confirmations"],
    }


def _resolve(items, item_id, name):
    normalized = name.casefold().strip()
    matches = [item for item in items if item.id == item_id] if item_id else [
        item for item in items if normalized and normalized in {n.casefold() for n in [item.name, *item.aliases]}
    ]
    if len(matches) != 1:
        return None
    item = matches[0]
    if normalized and normalized not in {n.casefold() for n in [item.name, *item.aliases]}:
        return None
    return item


def _parse_date(value):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid calendar date")
    return date.fromisoformat(value)


def _add_months(value: date, months: int) -> date:
    """Explicit calendar-clamped semantics: Jan 31 + 1 month is Feb 28/29."""
    ordinal = value.year * 12 + value.month - 1 + months
    year, month0 = divmod(ordinal, 12)
    month = month0 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def normalized_hostname(value: str) -> str:
    """Parse only. Never fetch or resolve a hostname; disallow ambiguous credential URLs."""
    if not value or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid URL characters")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname:
        raise ValueError("Absolute HTTP(S) URL required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credential-bearing URL is not accepted")
    # Force urllib to validate malformed port values even though ports do not identify a host.
    _ = parsed.port
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if not host or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                       or not all(c.isalnum() or c == "-" for c in label) for label in host.split(".")):
        raise ValueError("Invalid hostname")
    if len(host) > 253:
        raise ValueError("Invalid hostname length")
    return host


def _response(code, reason, outcome="no_issue", status="completed", next_step="確認填答是否正確；正式申請仍需核對文件。"):
    return {"reason_code": code, "reason": reason, "message": reason, "outcome": outcome,
            "execution_status": status, "next_step": next_step}


def _pending(code, reason):
    return _response(code, reason, None, "pending", "補充或修改填答，或洽承辦確認最新規則後重新預檢。")


def _review(code, reason):
    return _response(code, reason, "manual_review", next_step="保留方案完整名稱與交易資料，洽承辦人工確認；不因此拒收案件。")


def _issue(code, reason):
    return _response(code, reason, "action_needed", next_step="查看規則依據與填答，必要時修改；正式適用性仍由承辦確認，不自動拒收。")


def evaluate(data: PrecheckInput, bundle: PrecheckBundle, *, now: datetime | None = None, demo_allowed=True) -> dict:
    timestamp = now or datetime.now(TAIPEI)
    timestamp = timestamp.replace(tzinfo=TAIPEI) if timestamp.tzinfo is None else timestamp.astimezone(TAIPEI)
    if bundle.snapshot:
        return _evaluate_public(data, bundle, timestamp, demo_allowed=demo_allowed)
    today = timestamp.date()
    rules, params = bundle.rules, bundle.rules.params
    block = _rule_block(bundle, demo_allowed, today)
    values = data.model_dump(mode="json")
    checks = []
    tool = _resolve(bundle.tools, data.tool_id, data.tool_name)
    plan = _resolve(tool.plans, data.plan_id, data.plan_name) if tool else None
    application = next((a for a in params.application_types if a.id == data.application_type), None)
    purchased = data.purchase_stage == "purchased"

    def tool_unavailable():
        if tool is None:
            return _review("unknown_tool", "工具未收錄或名稱與選項不一致，不能自動判為不符合。")
        if tool.status == "draft" or tool.status != rules.status:
            return _pending("catalog_unconfirmed", "工具目錄尚未確認，或目錄與規則的使用模式不一致。")
        if not tool.valid_from <= today <= tool.valid_until:
            return _pending("catalog_out_of_period", "工具目錄已過期或尚未生效。")
        return None

    def add(check_id, title, fields, calculate, applicable=True, required=True):
        check = {
            "check_id": check_id, "title": title, "evidence_type": "self_report",
            "rule_id": "demo:" + check_id, "executed_at": timestamp.isoformat(),
            "related_fields": fields, "triggered_by": {key: values[key] for key in fields},
            "rule_version": rules.version, "source": rules.source.model_dump(mode="json"),
            "required": required and applicable,
        }
        if not applicable:
            answer = _response("not_applicable", "此購買情境不需執行此項檢查。", None, "not_applicable", "依目前情境準備即可。")
        elif block:
            answer = _pending(*block)
        else:
            try:
                answer = calculate()
            except (ValueError, OverflowError):
                answer = _response("invalid_input", "日期或網址格式無法解析；本項尚未完成。", None, "failed", "返回修改為有效日期或完整網址，再重新預檢。")
            except Exception:
                # Preserve an incomplete result, without leaking personal inputs into exceptions/logs.
                answer = _response("execution_failed", "本項規則執行失敗，不能視為沒有異常。", None, "failed", "稍後重新預檢，或洽承辦確認。")
        check.update(answer)
        checks.append(check)

    def check_tool():
        if not data.tool_id and not data.tool_name:
            return _pending("missing_tool", "尚未選擇或填寫工具名稱。")
        unavailable = tool_unavailable()
        if unavailable:
            return unavailable
        if tool.restricted:
            return _issue("tool_restricted", "依填答命中規則限制／需確認：" + tool.restriction_reason)
        return _response("tool_listed", "工具已收錄；僅代表目錄可比對，不代表整筆交易適用。")

    def check_plan():
        if not data.plan_id and not data.plan_name:
            return _pending("missing_plan", "尚未提供完整方案名稱。")
        unavailable = tool_unavailable()
        if unavailable:
            return unavailable
        if not plan:
            return _review("unknown_plan", "方案未收錄或名稱與選項不一致，保留人工確認。")
        return _response("plan_listed", "方案已收錄，仍需核對計費、通路及期間。")

    def check_billing():
        if data.billing_type == "unsure":
            return _pending("billing_unsure", "尚未確認計費方式。")
        unavailable = tool_unavailable()
        if unavailable:
            return unavailable
        if not plan:
            return _review("billing_plan_unknown", "未確認方案分類，不能只靠品牌或 Credit／Token 字樣判斷。")
        if data.billing_type not in plan.billing_types:
            return _review("billing_conflict", "填答的計費方式與已收錄方案不一致，需確認實際交易。")
        if plan.classification == "standalone_credits" and params.restrict_standalone_credits:
            return _issue("standalone_credits_restricted", "依填答命中規則限制／需確認：此方案分類為另外購買額度。")
        if plan.classification == "other":
            return _review("billing_unclassified", "此方案分類尚待確認。")
        return _response("billing_classified", "依目錄分類為訂閱內含額度，並非另外購買額度。" if
                         plan.classification == "subscription_included_credits" else "計費方式與已收錄方案一致。")

    def check_channel():
        if data.purchase_channel == "unsure":
            return _pending("channel_unsure", "尚未確認購買通路。")
        if data.purchase_channel in params.restricted_channels:
            return _issue("channel_restricted", "依填答命中規則通路限制／需確認，不代表案件自動拒收。")
        unavailable = tool_unavailable()
        if unavailable:
            return unavailable
        if data.purchase_channel not in tool.channels:
            return _review("channel_not_listed", "此工具的購買通路尚未收錄確認。")
        return _response("channel_listed", "使用者自述的通路已列於目錄；尚未核對交易文件。")

    def check_domain():
        if not data.purchase_url:
            return _pending("missing_url", "需填寫購買網址，才能比對已核對入口。")
        host = normalized_hostname(data.purchase_url)
        unavailable = tool_unavailable()
        if unavailable:
            return unavailable
        if any(host == entry.hostname or entry.include_subdomains and host.endswith("." + entry.hostname)
               for entry in tool.domains):
            return _response("domain_listed", "網址 hostname 符合目錄入口；不代表交易、軟體安全或供應商國別已查證。")
        return _review("domain_not_listed", "網址 hostname 不在已核對入口內，需人工確認購買來源。")

    def date_interval(value, lower, upper, missing_code, label):
        if not value:
            return _pending(missing_code, "尚未填寫" + label + "。")
        parsed = _parse_date(value)
        if lower <= parsed <= upper:
            return _response("date_in_range", f"使用者自述的{label}落於設定區間 {lower} 至 {upper}（含首尾日）。")
        return _issue("date_out_of_range", f"依填答命中規則日期限制／需確認：{label}未在 {lower} 至 {upper} 內。")

    def subscription_period():
        unavailable = tool_unavailable()
        if unavailable:
            return unavailable
        if not plan:
            return _review("subscription_plan_unknown", "方案分類未知，訂閱期間需人工確認。")
        if not data.subscription_start or not data.subscription_end:
            return _pending("missing_subscription_dates", "尚未填妥訂閱開始與結束日期。")
        start, end = _parse_date(data.subscription_start), _parse_date(data.subscription_end)
        if end < start:
            return _issue("subscription_reversed", "訂閱結束日期早於開始日期，請修改填答。")
        if params.subscription_month_semantics != "calendar_clamped" or (
            params.subscription_min_months is None and params.subscription_max_months is None
        ):
            return _pending("month_rule_ambiguous", "尚無明確月份計算規則，不能直接以每月 30 天推算。")
        if params.subscription_min_months is not None and end < _add_months(start, params.subscription_min_months):
            return _issue("subscription_too_short", "依設定的日曆月規則，訂閱期間短於所需月份／需確認。")
        if params.subscription_max_months is not None and end > _add_months(start, params.subscription_max_months):
            return _issue("subscription_too_long", "依設定的日曆月規則，訂閱期間超過設定月份／需確認。")
        return _response("subscription_period_in_range", "期間符合設定的日曆月計算，月底採該月最後一日；尚未核對訂閱文件。")

    add("tool", "工具收錄情形", ["tool_id", "tool_name"], check_tool)
    add("plan", "方案收錄情形", ["plan_id", "plan_name"], check_plan)
    add("billing", "訂閱與額度分類", ["billing_type", "plan_id", "plan_name"], check_billing)
    add("channel", "購買通路", ["purchase_channel"], check_channel)
    add("domain", "購買入口比對", ["purchase_url", "tool_id"], check_domain,
        applicable=data.purchase_channel == "official" or bool(data.purchase_url))
    add("seller", "購買來源資料", ["purchase_channel", "seller", "purchase_url"],
        lambda: _response("seller_supplied", "已提供購買來源，內容尚未核對。") if data.seller or data.purchase_url
        else _pending("missing_seller", "請提供購買網址或賣方，以供人工確認。"),
        applicable=data.purchase_channel not in {"official", "unsure"})
    add("purchase_date", "購買日期", ["purchase_date"],
        lambda: date_interval(data.purchase_date, params.purchase_date_from, params.purchase_date_until,
                              "missing_purchase_date", "購買日期"), applicable=purchased)
    add("subscription_period", "訂閱期間", ["subscription_start", "subscription_end"], subscription_period,
        applicable=purchased and (plan is None or plan.classification != "standalone_credits"))
    add("residency", "設籍自查", ["residency"],
        lambda: _pending("residency_unsure", "設籍情形仍不確定。") if data.residency == "unsure" else
        _response("residency_self_report", "使用者自述設籍情形符合設定；戶籍真實性尚未核對。") if
        data.residency == params.residency else _issue("residency_outside", "依自述設籍情形命中規則限制／需確認。"))
    add("birth_date", "出生日期自查", ["birth_date"],
        lambda: date_interval(data.birth_date, params.birth_date_from, params.birth_date_until,
                              "missing_birth_date", "出生日期"))
    add("application_type", "申請類型", ["application_type"],
        lambda: _response("application_type_listed", "已依申請類型列出需準備資料。") if application else
        _pending("missing_application_type", "請選擇設定中的申請類型。"))
    add("payer", "付款人自述", ["payer"],
        lambda: _pending("payer_unsure", "尚未確認付款人。") if data.payer == "unsure" else
        _response("payer_self_report", "付款人僅為使用者自述，付款一致性尚未核對。"), applicable=purchased)
    add("payer_relationship", "代付關係說明", ["payer", "payer_relationship"],
        lambda: _review("payer_relationship_review", "已提供代付關係，需備妥代付說明並由承辦確認；不因此拒收。") if
        data.payer_relationship else _pending("missing_payer_relationship", "請補充他人代付的關係說明。"),
        applicable=purchased and data.payer == "other")

    documents = []
    if block is None:
        requirements = list(params.base_documents)
        if application:
            requirements += application.documents
        if purchased:
            requirements += params.purchased_documents
            if data.payer == "other":
                requirements += params.paid_by_other_documents
        documents = [{**doc.model_dump(), "status": "self_reported_ready" if doc.doc_id in data.prepared_documents
                      else "needed", "received": False, "verified": False} for doc in requirements]
    required_checks = [check for check in checks if check["required"]]
    complete = sum(check["execution_status"] == "completed" for check in required_checks)
    issues = sum(check["outcome"] in {"action_needed", "manual_review"} for check in required_checks)
    incomplete = len(required_checks) - complete
    message = "已執行的自填預檢未發現異常；尚未核對文件，不代表資格已驗證。"
    if incomplete:
        message = f"尚有 {incomplete} 項必要檢查未完成，不能判定整體無異常；請補充或洽承辦確認。"
    elif issues:
        message = f"已完成自填預檢，其中 {issues} 項需處理或人工確認；不代表案件拒收。"
    if rules.status == "demo":
        message = "DEMO 演示結果（合成規則）｜" + message
    disclaimers = list(DISCLAIMERS)
    if rules.status == "demo":
        disclaimers.insert(0, "DEMO 演示模式：工具、方案與規則均為合成資料，不代表政府公告。")
    if block:
        disclaimers.insert(0, block[1])
    return {
        "mode": rules.status, "demo": rules.status == "demo", "rules_version": rules.version,
        "catalog_version": bundle.catalog_version, "input_version": bundle.input_version,
        "executed_at": timestamp.isoformat(), "checks": checks,
        "summary": {"required_total": len(required_checks), "completed": complete,
                    "incomplete": incomplete, "issues": issues, "message": message},
        "documents": documents, "uncovered_checks": list(UNCOVERED), "disclaimers": disclaimers,
        "official_application_url": bundle.official_application_url,
    }


def _public_source_notice(bundle):
    snapshot = bundle.snapshot
    return (f"公開來源預檢｜來源核對日期 {snapshot.source_checked_date}，快照 {snapshot.snapshot_version}。"
            "已核對公開網頁文字，不代表讀過全部附件或機關已確認系統規格；不自動核定或拒收。")


def _public_documents(data, bundle, application, selected_categories):
    params = bundle.rules.params
    requirements = [doc.model_dump() for doc in params.base_documents]
    if application:
        requirements += [doc.model_dump() for doc in application.documents if doc.doc_id != "D06"]
    if data.application_type in {"specific", "language"}:
        choices = "、".join(category.name for category in selected_categories)
        requirements.append({
            "doc_id": "D06", "title": "特定對象／語言認證證明（擇一適用類別）",
            "reason": (f"依自述可提供 {choices} 中任一適用類別的證明；不要求全部身分。" if choices else
                       "請先確認適用的特定對象或語言認證類別，再準備相應證明；不要求全部身分。")
                      + "身分證明待確認，不自行增加證照級別或有效期限。",
        })
    if data.purchase_stage == "purchased":
        requirements += [doc.model_dump() for doc in params.purchased_documents
                         if not data.transactions or doc.doc_id not in {"D02", "D03"}]
        if data.payer == "other":
            requirements += [doc.model_dump() for doc in params.paid_by_other_documents]
        for index, transaction in enumerate(data.transactions, start=1):
            for doc_id, title, reason in [
                ("D02", "官方訂閱收據／憑證", "確認訂閱人、完整工具與公司名稱、日期、期間、原始費用及付款方式。"),
                ("D03", "臺幣帳單及繳款佐證", "逐月提供臺幣金額與相應繳款資料；不以即時匯率代替核銷金額。"),
            ]:
                requirements.append({"doc_id": f"{doc_id}_tx_{index}", "title": f"第 {index} 筆：{title}",
                                     "reason": reason, "transaction_index": index,
                                     "transaction_purchase_date": transaction.purchase_date})
    for requirement in requirements:
        if requirement["doc_id"] == "D03" or requirement["doc_id"].startswith("D03_tx_"):
            requirement["reason"] += (
                "信用卡付款正式申辦另需末四碼與持卡人姓名照片；本預檢不收集完整卡號、CVV 或 OTP。"
                if data.payment_method == "credit_card" else
                "非信用卡付款的替代佐證尚待承辦確認，不自動豁免付款證明。"
            )
    unique = {requirement["doc_id"]: requirement for requirement in requirements}
    return [{**requirement, "status": "self_reported_ready" if requirement["doc_id"] in data.prepared_documents
             else "required", "required": True, "received": False, "content_unverified": True,
             "reviewed": False, "verified": False} for requirement in unique.values()]


def _monthly_row_conflict(transactions):
    """Do not count obviously duplicate or overlapping rows twice as eligible costs."""
    observed = set()
    periods = []
    for transaction in transactions:
        key = (transaction.purchase_date, transaction.subscription_start, transaction.subscription_end)
        if any(key) and key in observed:
            return _review("duplicate_monthly_transaction", "逐月交易有重複日期與期間，需確認是否重複填列；暫不累加試算。")
        observed.add(key)
        if transaction.subscription_start and transaction.subscription_end:
            period = (_parse_date(transaction.subscription_start), _parse_date(transaction.subscription_end))
            if any(max(period[0], previous[0]) < min(period[1], previous[1]) for previous in periods):
                return _review("overlapping_monthly_transactions", "逐月訂閱期間重疊，需核對是否重複費用或不同品項；暫不累加試算。")
            periods.append(period)
    return None


def _evaluate_public(data, bundle, timestamp, *, demo_allowed):
    """Evaluate explicit public conditions independently; uncertainty is confined to its own check."""
    rules, params = bundle.rules, bundle.rules.params
    block = _rule_block(bundle, demo_allowed, timestamp.date())
    purchased = data.purchase_stage == "purchased"
    values = data.model_dump(mode="json")
    checks = []
    tool = _resolve(bundle.tools, data.tool_id, data.tool_name)
    plan = _resolve(tool.plans, data.plan_id, data.plan_name) if tool else None
    application = next((item for item in params.application_types if item.id == data.application_type), None)
    categories = {category.id: category for category in params.qualification_categories}
    selected_categories = [categories[key] for key in dict.fromkeys(data.qualification_categories) if key in categories]
    enhanced = data.application_type in {"specific", "language"}
    rate = params.enhanced_rate if enhanced else params.general_rate
    cap = params.enhanced_cap_twd if enhanced else params.general_cap_twd
    estimate = {"available": False, "eligible_cost_twd": None,
                "rate": format(rate, "f") if rate is not None else None,
                "cap_twd": format(cap, "f") if cap is not None else None, "estimated_subsidy_twd": None,
                "currency": "TWD", "conditional": True, "label": "依填答試算",
                "proof_pending": enhanced, "rounding": "not_applied", "reason": "尚無可試算的已知臺幣費用。"}

    def supplied(field):
        current = values
        for part in field.split("."):
            current = current[int(part)] if isinstance(current, list) else current.get(part)
        return current

    def add(check_id, rule_id, title, fields, calculate, applicable=True, required=True):
        check = {"check_id": check_id, "rule_id": rule_id, "title": title, "evidence_type": "self_report",
                 "related_fields": fields, "triggered_by": {field: supplied(field) for field in fields},
                 "rule_version": rules.version, "source": rules.source.model_dump(mode="json"),
                 "required": applicable and required, "executed_at": timestamp.isoformat()}
        if not applicable:
            answer = _response("not_applicable", "此情境不需執行本項預檢。", None, "not_applicable", "依目前情境準備即可。")
        elif block:
            answer = _pending(*block)
        else:
            try:
                answer = calculate()
            except (ValueError, OverflowError):
                answer = _response("invalid_input", "日期、網址或金額無法解析，本項尚未完成。", None, "failed",
                                   "返回修改有效日期、完整網址或金額，再重新預檢。")
            except Exception:
                answer = _response("execution_failed", "本項規則執行失敗，不能視為沒有異常。", None, "failed",
                                   "重新預檢或洽承辦確認。")
        check.update(answer)
        checks.append(check)

    def catalog_available():
        if tool is None:
            return _review("unknown_tool", "工具尚未列名或填答與目錄不一致，需人工確認，不能直接排除。")
        if tool.source.kind != "official" or tool.status == "demo":
            return _pending("catalog_unconfirmed", "此目錄項目沒有已核對的正式公開來源。")
        if not tool.valid_from <= timestamp.date() <= tool.valid_until:
            return _pending("catalog_out_of_period", "目錄項目已過期或尚未生效，需確認適用版本。")
        return None

    def check_tool():
        if not data.tool_id and not data.tool_name:
            return _pending("missing_tool", "尚未選擇或填寫完整 AI 工具名稱。")
        unavailable = catalog_available()
        if unavailable:
            return unavailable
        if tool.catalog_status == "excluded_by_notice":
            return _issue("tool_excluded_by_notice", "依填答命中本計畫限制：此工具列於公告排除名單。"
                          "這是本計畫的行政限制，未推定公司的當前國籍或資安風險，不自動拒收。")
        if tool.catalog_status == "listed_example":
            return _response("tool_listed_example", "工具為公告列舉項目；不是保證核准的白名單，仍須檢查方案、通路及日期。")
        return _review("tool_catalog_unverified", "工具尚待依公開公告確認，不能只靠品牌或國別推定適用。")

    def check_plan():
        if not data.plan_name and not data.plan_id:
            return _pending("missing_plan", "需填寫完整 AI 訂閱方案名稱；一般雲端空間或同品牌非 AI 服務不自動對應。")
        if plan and tool and tool.catalog_status == "verified_plan_catalog" and catalog_available() is None:
            return _response("plan_listed", "方案已有目錄紀錄，交易內容及文件仍需核對。")
        return _review("unknown_plan", "公開公告列舉工具，未提供此完整方案的適用認定，需人工確認；不直接排除。")

    def check_billing():
        component = data.billing_component
        if plan and tool and tool.catalog_status == "verified_plan_catalog" and catalog_available() is None:
            expected = {"subscription": {"subscription"},
                        "subscription_included_credits": {"subscription", "included_credits"},
                        "standalone_credits": {"standalone_credits"}}.get(plan.classification)
            if not expected or component not in expected or data.billing_type not in plan.billing_types:
                return _review("billing_plan_conflict", "填答的計費種類或額度分類與已核對方案不同，需確認實際購買品項，暫不認列費用。")
        if component == "unsure":
            return _review("billing_classification_unknown", "尚未確認訂閱費與額外額度的實際分類，不能以商品名稱關鍵字判斷。")
        if component == "standalone_credits":
            return _issue("standalone_credits_restricted", "依填答命中本計畫限制：預付儲值、單獨 Credit／Token／API 額度或按量扣抵服務不在公告補助範圍。")
        if component == "mixed":
            return _review("mixed_billing_review", "訂閱與額外額度為混合品項，需拆分費用並確認認列方式，不能把全部金額直接納入。")
        if data.billing_type not in {"monthly", "annual"}:
            return _review("billing_type_conflict", "自述為訂閱，但計費週期仍不明或與額度計費衝突，需確認。")
        return _response("subscription_included_credits" if component == "included_credits" else "subscription_classified",
                         "使用者自述為訂閱內含額度，不因 Credit／Token 字樣直接排除；額外加購仍需分列。" if
                         component == "included_credits" else "使用者自述為月／年訂閱，與額外儲值分開判斷，尚未核對憑證。")

    def check_channel():
        if data.purchase_channel == "official":
            return _response("official_channel_self_report", "使用者自述直接向官方網站購買；網址及交易憑證仍需核對，不代表已認證來源。")
        if data.purchase_channel in params.restricted_channels:
            return _issue("channel_restricted", "依填答命中本計畫限制：集合平台或代購不符合直接向 AI 軟體官網購買的公告要求；不自動拒收。")
        if data.purchase_channel == "app_store":
            return _review("app_store_review", "App Store／Google Play 為非官網通路，尚無已確認例外，不能保證適用，需人工確認。")
        return _review("channel_unknown", "購買通路尚未確認為直接向官方網站購買，需提供來源資料供人工確認。")

    def check_domain():
        if not data.purchase_url:
            return _pending("missing_url", "需補充購買網址或可供核對的來源資料。")
        host = normalized_hostname(data.purchase_url)
        if not tool or not tool.domains or catalog_available() is not None:
            return _review("domain_not_verified", "此工具的官方購買網域尚未查證；僅完成網址解析，沒有抓取網頁或推定供應商國別。")
        if any(host == domain.hostname or domain.include_subdomains and host.endswith("." + domain.hostname)
               for domain in tool.domains):
            return _response("domain_listed", "hostname 符合已核對入口；不代表交易或軟體安全已查證。")
        return _review("domain_not_listed", "hostname 與已核對入口不符，需人工確認，不以 substring 比對。")

    def interval(value, lower, upper, label):
        if not value:
            return _pending("missing_date", f"尚未填寫{label}。")
        parsed = _parse_date(value)
        if lower <= parsed <= upper:
            return _response("date_in_range", f"自述{label}在 {lower} 至 {upper}（含首尾）內；未核對文件。")
        return _issue("date_out_of_range", f"依填答命中本計畫限制：{label}不在 {lower} 至 {upper} 範圍，請確認填答；不自動拒收。")

    def check_application():
        if not application:
            return _pending("missing_application_type", "請選擇一般青年、特定對象或文化語言保存者類別。")
        if not enhanced:
            if data.qualification_categories:
                return _review("category_choice_conflict", "選擇一般類別且填有加碼身分，請確認欲採用的類別；不自動疊加比例。")
            return _response("general_category", "使用者自述為一般青年，以設定的一般補助比例與上限作條件式試算。")
        if not selected_categories or any(key not in categories for key in data.qualification_categories):
            return _review("qualification_unknown", "加碼身分或證明種類仍不明，請確認適用類別並準備相應證明。")
        if not any(category.kind == data.application_type for category in selected_categories):
            return _review("qualification_category_conflict", "所選證明種類與申請類型不一致，需確認欲採用的身分。")
        return _review("qualification_proof_pending", "依自述可作 90% 類別的條件式試算；身分證明待確認。"
                       "多種身分不疊加比例或上限，擇一提供適用證明即可。")

    def check_amount():
        if data.foreign_currency_only:
            estimate["reason"] = "只有外幣資料，需臺幣帳單／換算及付款佐證；不使用當天匯率產生核銷金額。"
            return _review("twd_evidence_missing", estimate["reason"])
        amount = data.eligible_cost_twd
        conflict = _monthly_row_conflict(data.transactions)
        if conflict:
            estimate["reason"] = conflict["reason"]
            return conflict
        rows = [transaction.eligible_cost_twd for transaction in data.transactions]
        if rows and all(value is not None for value in rows):
            total = sum(rows, Decimal("0"))
            if amount is not None and amount != total:
                estimate["reason"] = "填答總額與逐月臺幣金額合計不一致，需確認費用。"
                return _review("amount_total_conflict", estimate["reason"])
            amount = total if amount is None else amount
        if amount is None:
            estimate["reason"] = "合格臺幣費用未知，不能當成 0；請補充臺幣帳單及付款資料。"
            return _pending("eligible_cost_unknown", estimate["reason"])
        if not application or rate is None or cap is None:
            estimate["reason"] = "申請類型或試算比例尚未明確，無法試算。"
            return _pending("estimate_rule_unknown", estimate["reason"])
        if enhanced and (not selected_categories or any(key not in categories for key in data.qualification_categories)
                         or not any(category.kind == data.application_type for category in selected_categories)):
            estimate["reason"] = "加碼身分或證明種類尚不明，請先確認適用類別。"
            return _review("estimate_qualification_unknown", estimate["reason"])
        if check_billing()["outcome"] != "no_issue":
            estimate["reason"] = "費用包含排除額度或分類／拆分不明，尚不能認列為合格訂閱費用。"
            return _review("estimate_cost_classification_unknown", estimate["reason"])
        if data.purchase_channel in params.restricted_channels or (tool and tool.catalog_status == "excluded_by_notice"):
            estimate["reason"] = "填答命中工具或通路限制，應先確認費用是否適用，暫不作補助金額試算。"
            return _review("estimate_restriction_review", estimate["reason"])
        calculated = min(amount * rate, cap)
        estimate.update({"available": True, "eligible_cost_twd": format(amount, "f"),
                         "estimated_subsidy_twd": format(calculated, "f"),
                         "reason": "依自述臺幣費用與類別試算，不是核定金額；身分、費用及文件均待核對。"
                         "未套用任何小數捨入規則，手續費、混合品項與年費認列方式待確認。"})
        return _response("conditional_estimate", estimate["reason"])

    def acceptance_window():
        if params.acceptance_date_from is None or params.acceptance_date_until is None:
            return _pending("acceptance_window_unknown", "尚無明確受理期間設定。")
        if params.acceptance_date_from <= timestamp.date() <= params.acceptance_date_until:
            return _response("acceptance_calendar_in_range", f"預檢日期落於公告受理期間，最晚至 {params.acceptance_date_until}；"
                             "經費用罄可提前結束，即時預算未知，不保留期限或額度。")
        return _review("outside_acceptance_window", "預檢日期已超出公告受理期間，請至正式入口確認；預檢日期不是正式送件日期。")

    months = params.monthly_application_months if data.billing_type == "monthly" else (
        params.annual_application_months if data.billing_type == "annual" else None)
    annual_cutoff = params.acceptance_date_until.isoformat() if params.acceptance_date_until else None

    def deadline_check():
        if months is None:
            return _review("purchase_deadline_unknown", "計費種類或購買後申請期限未確認。")
        return _review("purchase_deadline_semantics_pending", f"公告要求{('月' if data.billing_type == 'monthly' else '年')}費產品"
                       f"於購買後 {months} 個月內申請，同時不得超過年度受理截止 {annual_cutoff or '待確認'}。"
                       "起算、月末及休假日處理方式待機關確認，不換算為固定天數，也不自行判定正式逾期。")

    def period_check(start_text, end_text):
        if not start_text or not end_text:
            return _pending("missing_subscription_dates", "尚未填妥訂閱期間或續訂日期。")
        start, end = _parse_date(start_text), _parse_date(end_text)
        if end < start:
            return _issue("subscription_reversed", "訂閱結束日期早於開始日期，請更正填答。")
        if data.billing_type == "monthly" and (start.year, start.month) != (end.year, end.month):
            return _review("cross_month_ambiguity", "月費訂閱跨月；公告的跨月文字與累積月份適用方式待確認，不自行判為不補助。")
        return _response("subscription_dates_self_report", "已提供訂閱期間自述；尚未核對官方憑證與費用認列。")

    def prior_check():
        if data.prior_subsidy == "received":
            return _issue("prior_subsidy_received", "依填答命中本計畫限制：自述本年度已受本計畫補助，公告每人每年最多一次；仍需有權紀錄核對。")
        if data.prior_subsidy in {"applied", "unsure"}:
            return _review("prior_application_review", "已有申請中紀錄或補助情形不明，需確認正式狀態；不能將草稿或送出直接視為已領補助。")
        return _response("prior_subsidy_self_report", "自述未領取、只有草稿、已撤回或未核准均不等於已領取補助；尚未排除跨機關重複補助。")

    def payer_relationship():
        if not data.payer_relationship:
            return _pending("missing_payer_relationship", "請說明由父母、配偶、法定代理人或其他關係代付。")
        if data.payer_relationship in {"parent", "spouse", "legal_guardian"}:
            return _review("payer_relationship_review", "父母／配偶／法定代理人代付須準備關係證明及共同切結書；"
                           "公告段落用語不同，具體資格待人工確認，不一律拒絕或自動核准。")
        return _review("other_payer_review", "其他代付關係未有已確認適用規則，不可自行放寬；請洽承辦確認，收款帳戶仍須為申請人本人。")

    def payment_method():
        if data.payment_method == "credit_card":
            return _response("credit_card_evidence_needed", "正式申辦另要求信用卡末四碼與姓名照片及臺幣繳款佐證；"
                             "本預檢不收集卡號、CVV、OTP 或證件。")
        return _review("payment_alternative_review", "非信用卡付款的替代佐證或付款方式尚未確認，不擅自豁免證明要求。")

    add("tool", "R12", "公告工具列舉／排除", ["tool_id", "tool_name"], check_tool)
    add("plan", "R12", "完整方案待核對", ["plan_id", "plan_name"], check_plan)
    add("billing", "R14", "訂閱與額外額度分類", ["billing_type", "billing_component"], check_billing)
    add("channel", "R13", "官網購買通路", ["purchase_channel", "seller"], check_channel)
    add("domain", "R13", "購買網址入口", ["purchase_url", "tool_id"], check_domain,
        applicable=data.purchase_channel == "official" or bool(data.purchase_url))
    add("residency", "R01", "設籍新竹市自查", ["residency"],
        lambda: _review("residency_unsure", "設籍情形不確定，需人工確認戶籍資料。") if data.residency == "unsure" else
        _response("residency_self_report", "使用者自述設籍新竹市；尚未查驗戶籍，LINE 登入不能作為戶籍證明。") if
        data.residency == "hsinchu" else _issue("residency_outside", "依填答命中本計畫限制：須設籍新竹市，新竹縣及其他地區不能視為新竹市。"))
    add("birth_date", "R02", "公告出生日期區間", ["birth_date"],
        lambda: interval(data.birth_date, params.birth_date_from, params.birth_date_until, "出生日期"))
    application_rule = {"specific": "R04", "language": "R05"}.get(data.application_type, "R03")
    add("application_type", application_rule, "補助類別及身分證明", ["application_type", "qualification_categories"], check_application)
    add("amount", "R06", "依填答試算", ["eligible_cost_twd", "foreign_currency_only", "transactions"], check_amount,
        applicable=purchased or data.eligible_cost_twd is not None or data.foreign_currency_only
        or any(row.eligible_cost_twd is not None for row in data.transactions), required=purchased)
    add("purchase_date", "R07", "購買期間", ["purchase_date"],
        lambda: interval(data.purchase_date, params.purchase_date_from, params.purchase_date_until, "購買日期"),
        applicable=purchased and not data.transactions)
    for index, transaction in enumerate(data.transactions):
        add(f"transaction_{index + 1}_purchase_date", "R07", f"第 {index + 1} 筆購買日期", [f"transactions.{index}.purchase_date"],
            lambda row=transaction: interval(row.purchase_date, params.purchase_date_from, params.purchase_date_until, "購買日期"),
            applicable=purchased)
        add(f"transaction_{index + 1}_period", "R10", f"第 {index + 1} 筆訂閱期間",
            [f"transactions.{index}.subscription_start", f"transactions.{index}.subscription_end"],
            lambda row=transaction: period_check(row.subscription_start, row.subscription_end), applicable=purchased)
    add("subscription_period", "R10", "訂閱期間自述", ["subscription_start", "subscription_end"],
        lambda: period_check(data.subscription_start, data.subscription_end),
        applicable=purchased and not data.transactions and data.billing_component != "standalone_credits")
    add("acceptance_window", "R08", "受理期間與預算狀態", [], acceptance_window)
    checks[-1]["evidence_type"] = "system_time"
    checks[-1]["execution_context"] = {"evaluated_on": timestamp.date().isoformat(), "timezone": "Asia/Taipei"}
    add("purchase_deadline", "R09", "購買後申請期限提醒", ["billing_type", "purchase_date"], deadline_check, applicable=purchased)
    add("monthly_accumulation", "R10", "連續月費累積", ["transactions", "billing_type"],
        lambda: _monthly_row_conflict(data.transactions) or _review("monthly_accumulation_review", "已提供多筆逐月交易，需逐月收據、臺幣及繳款佐證。"
                        "連續月份、最後一月申請及跨月文字的適用關係待人工確認；不把每張收據當成一次補助。")
        if data.billing_type == "monthly" else _review("transaction_type_conflict", "多筆逐月交易與所選計費類型不一致，需人工確認。"),
        applicable=purchased and len(data.transactions) > 1)
    add("prior_subsidy", "R11", "年度次數與重複自查", ["prior_subsidy"], prior_check)
    add("multiple_tools", "R11", "一案多工具", ["multiple_tools"],
        lambda: _review("multiple_tools_review", "一案多工具與公告一次購買限制的適用關係尚未確認，需人工判斷，不能自動合併或排除。"),
        applicable=data.multiple_tools)
    add("payer", "D07", "付款人自述", ["payer"],
        lambda: _pending("payer_unsure", "請確認付款人。") if data.payer == "unsure" else
        _response("payer_self_report", "付款人為使用者自述，尚未核對付款一致性；補助收款帳戶仍須為申請人本人。"), applicable=purchased)
    add("payer_relationship", "D07", "代付資格及備件", ["payer", "payer_relationship"], payer_relationship,
        applicable=purchased and data.payer == "other")
    add("payment_method", "D03", "付款佐證種類", ["payment_method"], payment_method, applicable=purchased)

    required_checks = [check for check in checks if check["required"]]
    complete = sum(check["execution_status"] == "completed" for check in required_checks)
    incomplete = len(required_checks) - complete
    issues = sum(check["outcome"] in {"action_needed", "manual_review"} for check in required_checks)
    message = "已執行的自填預檢未發現異常；尚未核對文件，不代表資格已驗證。"
    if incomplete:
        message = f"尚有 {incomplete} 項必要檢查未完成，不能判定整體無異常；請補充或洽承辦確認。"
    elif issues:
        message = f"自填預檢有 {issues} 項需處理或人工確認，不表示正式申請已核定或拒收。"
    if block:
        estimate["reason"] = block[1]
    disclaimers = [_public_source_notice(bundle), *DISCLAIMERS]
    disclaimers += ["目前套用現行公開快照；回算 2026-08-14 以前的歷史案件需另取得當時規則版本。",
                   "即時補助經費未知；預檢不保留額度或期限。",
                   "沒有有效機關通知與政府辦公日曆，正式補件期限待確認；預檢修正不啟動倒數或行政處分。"]
    if block:
        disclaimers.insert(0, block[1])
    return {
        "mode": "public_advisory", "demo": False, "rules_version": rules.version,
        "catalog_version": bundle.catalog_version, "input_version": bundle.input_version,
        "snapshot": bundle.snapshot.model_dump(mode="json"), "sources": [s.model_dump(mode="json") for s in bundle.sources],
        "executed_at": timestamp.isoformat(), "checks": checks,
        "summary": {"required_total": len(required_checks), "completed": complete,
                    "incomplete": incomplete, "issues": issues, "message": "公開來源預檢｜" + message},
        "documents": _public_documents(data, bundle, application, selected_categories) if block is None else [],
        "uncovered_checks": [*UNCOVERED, "跨機關重複補助紀錄", "即時經費是否用罄", "正式補正通知與政府辦公日曆"],
        "disclaimers": disclaimers, "estimate": estimate, "funding_status": "unknown",
        "pending_confirmations": [item.model_dump(mode="json") for item in bundle.pending_confirmations],
        "deadline_reminder": {"months": months if purchased else None, "exact_due_date": None,
                              "annual_cutoff": annual_cutoff, "reason": "起算、月末及休假日處理方式待確認；不換算固定天數。"},
        "formal_submission": False, "formal_correction_deadline": None,
        "automatic_approval_enabled": False, "official_application_url": bundle.official_application_url,
        "transaction_fingerprint": hashlib.sha256(json.dumps(values["transactions"], sort_keys=True,
                                                              ensure_ascii=True).encode()).hexdigest(),
    }


def compare_results(previous_result: dict | None, current_result: dict) -> dict:
    previous = {check["check_id"]: check for check in (previous_result or {}).get("checks", [])}
    current = {check["check_id"]: check for check in current_result.get("checks", [])}
    issue_outcomes = {"action_needed", "manual_review"}

    def is_issue(check):
        return check.get("outcome") in issue_outcomes

    def is_pending(check):
        return check.get("execution_status") in {"pending", "failed"} or check.get("outcome") == "manual_review"

    result = {"new_issues": [], "resolved_issues": [], "still_pending": [], "lost_confirmation": [],
              "rule_changed": bool(previous_result and previous_result.get("rules_version") != current_result.get("rules_version")),
              "catalog_changed": bool(previous_result and previous_result.get("catalog_version") != current_result.get("catalog_version"))}
    transaction_changed = bool(previous_result and previous_result.get("transaction_fingerprint")
                               and previous_result.get("transaction_fingerprint") != current_result.get("transaction_fingerprint"))
    result["transaction_set_changed"] = transaction_changed
    result["transaction_changes_require_review"] = transaction_changed
    result["transaction_change_message"] = (
        "逐月交易內容或順序已變更；請逐筆核對前後快照，索引不同的交易不自動視為同筆問題已解除。"
        if transaction_changed else ""
    )
    for check_id, check in current.items():
        if transaction_changed and check_id.startswith("transaction_"):
            continue
        old = previous.get(check_id)
        if is_issue(check) and (not old or not is_issue(old)):
            result["new_issues"].append(check)
        if old and is_issue(old) and not is_issue(check) and check["execution_status"] in {"completed", "not_applicable"}:
            result["resolved_issues"].append(check)
        if is_pending(check) and (not old or is_pending(old)):
            result["still_pending"].append(check)
        if old and old.get("execution_status") == "completed" and (
            check.get("execution_status") in {"pending", "failed"}
            or old.get("outcome") == "no_issue" and check.get("outcome") == "manual_review"
        ):
            result["lost_confirmation"].append(check)
    return result
