"""Offline LINE UX / state tests. Real policy evaluation, fake identities, no network."""
import base64
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app import line_conversation as chat
from app.precheck_engine import TAIPEI, evaluate, load_bundle
from app.precheck_schema import PrecheckInput


NOW = datetime(2026, 9, 19, 12, tzinfo=TAIPEI)
ACTOR = "synthetic-actor-hmac"
SECRET = "synthetic-test-channel-secret"
WEB = "http://127.0.0.1:8765/precheck"


@pytest.fixture
def bundle():
    return load_bundle(Path(__file__).parents[1] / "app/data/precheck-hsinchu-115.json")


@pytest.fixture
def store():
    return chat.ConversationStore()


def reply(command, bundle, store, **kwargs):
    options = {"actor_key": ACTOR, "secret_key": SECRET, "now": NOW, "public_url": WEB, "store": store}
    return chat.build_reply(command, bundle=bundle, **{**options, **kwargs})


def text(messages):
    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"text", "altText"} and isinstance(value, str):
                    yield value
                elif isinstance(value, (dict, list)):
                    yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    return "\n".join(walk(messages))


def actions(messages):
    def walk(node):
        if isinstance(node, dict):
            if "action" in node:
                yield node["action"]
            for value in node.values():
                if isinstance(value, (dict, list)):
                    yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    return list(walk(messages))


def choose(messages, label):
    action = next(action for action in actions(messages) if action["label"] == label)
    return action.get("data", action.get("text"))


def assert_limits(messages):
    assert 1 <= len(messages) <= 5
    for message in messages:
        assert message["type"] in {"text", "flex"}
        if message["type"] == "text":
            assert 0 < len(message["text"].encode("utf-16-le")) // 2 <= 5000
        else:
            assert 0 < len(message["altText"].encode("utf-16-le")) // 2 <= 400
            assert len(json.dumps(message["contents"], ensure_ascii=False).encode()) < 30000
        assert len(message.get("quickReply", {}).get("items", [])) <= 13
    for action in actions(messages):
        assert len(action["label"].encode("utf-16-le")) // 2 <= 20
        if action["type"] == "postback":
            assert len(action["data"]) <= 300
            assert chat.normalize_event_command({"type": "postback", "postback": {"data": action["data"]}}) is not None


def flow(bundle, store, labels=None):
    messages = reply("預檢", bundle, store)
    for label in labels or ("已經購買", "ChatGPT", "月費訂閱", "軟體官方網站"):
        assert_limits(messages)
        messages = reply(choose(messages, label), bundle, store)
    assert_limits(messages)
    return messages


def handoff(messages):
    url = next(action["uri"] for action in actions(messages)
               if action["type"] == "uri" and "line_handoff=" in action["uri"])
    return parse_qs(urlsplit(url).query)["line_handoff"][0]


@pytest.mark.parametrize("command", sorted(chat.COMMANDS))
def test_exact_seven_commands_and_limits(bundle, store, command):
    assert chat.COMMANDS == {"選單", "預檢", "文件", "進度", "補正", "安全", "協助"}
    event = {"type": "message", "message": {"type": "text", "text": " " + command + "\n"}}
    assert chat.normalize_event_command(event) == command
    assert_limits(reply(command, bundle, store))


@pytest.mark.parametrize("alias,command", chat.STATIC_POSTBACKS.items())
def test_static_menu_contract(alias, command):
    assert chat.normalize_event_command({"type": "postback", "postback": {"data": alias}}) == command


@pytest.mark.parametrize("value", ["王小明 2000-01-01", "結果", "開始預檢", "補助規則", "其他人案件 https://example.com/cases/123",
                                  "選單 " + "x" * 500, "身分證 A123456789"])
def test_unrecognized_text_and_case_links_never_become_commands(bundle, store, value):
    assert chat.normalize_event_command({"type": "message", "message": {"type": "text", "text": value}}) is None
    messages = reply(value, bundle, store)
    assert value not in text(messages)
    assert "案件授權" in text(messages)
    assert_limits(messages)


@pytest.mark.parametrize("event", [None, {}, {"type": "message", "message": None},
    {"type": "message", "message": {"type": "image", "id": "private-image"}},
    {"type": "postback", "postback": {"data": "raw-private-name"}}, {"type": "postback", "postback": None}])
def test_media_and_malformed_events_are_not_retained(event):
    assert chat.normalize_event_command(event) is None


def test_follow_welcome_is_flex_and_six_menu_entries(bundle, store):
    assert chat.normalize_event_command({"type": "follow"}) == "選單"
    messages = reply("選單", bundle, store)
    assert messages[0]["type"] == "flex"
    assert [action["text"] for action in actions(messages)] == ["預檢", "文件", "進度", "補正", "安全", "協助"]
    assert "生日" in text(messages) and "不收" in text(messages)


def test_four_choices_only_then_opaque_web_handoff(bundle, store):
    messages = flow(bundle, store)
    token = handoff(messages)
    choices = chat.read_handoff(token, store=store, now=NOW)
    assert choices == {"purchase_stage": "purchased", "tool_id": "chatgpt", "billing_type": "monthly",
                       "billing_component": "subscription", "purchase_channel": "official"}
    assert "完整預檢尚未完成" in text(messages)
    assert "資格、期限或金額" in text(messages)
    assert "此連結不代表登入" in text(messages)
    serialized = json.dumps(store.sessions, ensure_ascii=False)
    assert "birth_date" not in serialized and "application_type" not in serialized and "payer" not in serialized
    assert bundle.rules.version in text(messages) and bundle.rules.source.url in text(messages)
    state = chat.conversation_state(ACTOR, store=store, now=NOW)
    assert state["revision"] == 4 and state["step"] == 4 and state["status"] == "awaiting_web"
    assert "answers" not in state and "handoff" not in state


@pytest.mark.parametrize("stage,tool", [("已經購買", "ChatGPT"), ("尚未購買", "其他／未列名工具")])
def test_full_desktop_flow_uses_same_signed_choices_without_quick_reply(bundle, store, stage, tool):
    current = reply("預檢", bundle, store)
    for label in (stage, tool, "月費訂閱", "軟體官方網站"):
        assert_limits(current)
        desktop = [{key: value for key, value in message.items() if key != "quickReply"} for message in current]
        mobile_choices = [item["action"] for message in current for item in message.get("quickReply", {}).get("items", [])
                          if item["action"]["type"] == "postback"]
        desktop_choices = [action for action in actions(desktop) if action["type"] == "postback"]
        assert desktop_choices == mobile_choices
        assert any(action.get("uri") == WEB for action in actions(desktop))
        assert "聊天選項不會帶入" in text(desktop)
        current = reply(choose(desktop, label), bundle, store)
    values = chat.read_handoff(handoff(current), store=store, now=NOW)
    assert values["purchase_stage"] == ("purchased" if stage == "已經購買" else "planning")
    assert values["tool_id"] == ("chatgpt" if tool == "ChatGPT" else None)


def test_desktop_fallback_does_not_copy_caller_query_or_fragment(bundle, store):
    messages = reply("預檢", bundle, store, public_url=WEB + "?name=private-name&caseId=private-case#private-token")
    urls = [action["uri"] for action in actions(messages) if action["type"] == "uri"]
    assert urls == [WEB]
    assert "private-" not in json.dumps(messages)


def test_desktop_choices_remain_available_without_configured_web_entry(bundle, store):
    messages = reply("預檢", bundle, store, public_url=None)
    desktop = [message for message in messages if message["type"] == "flex"]
    assert choose(desktop, "已經購買")
    assert not any(action["type"] == "uri" for action in actions(desktop))
    assert_limits(messages)


def test_rules_menu_displays_public_parameters_and_source_separately_from_help(bundle, store):
    rules = reply("youth:rules", bundle, store)
    help_messages = reply("協助", bundle, store)
    content = text(rules)
    assert rules != help_messages and rules[0]["type"] == "flex"
    assert chat.normalize_event_command({"type": "postback", "postback": {"data": "youth:rules"}}) == "_rules"
    for value in ("公開補助規則摘要", str(bundle.rules.params.birth_date_from), str(bundle.rules.params.birth_date_until),
                  str(bundle.rules.params.purchase_date_from), str(bundle.rules.params.purchase_date_until),
                  str(bundle.rules.params.acceptance_date_until), bundle.rules.version, bundle.rules.source.title):
        assert value in content
    assert any(action.get("uri") == bundle.rules.source.url for action in actions(rules))
    assert "未經機關核定" in content and "未知方案" in content and "即時經費未知" in content
    assert "目前沒有串接即時人工客服" in text(help_messages)
    assert "出生日期區間" not in text(help_messages)
    assert "3000" not in content and "6000" not in content and "low_income" not in content
    assert_limits(rules)


def test_rules_card_tracks_bundle_changes_and_labels_synthetic_or_expired_rules(bundle, store):
    revised = bundle.model_copy(deep=True)
    revised.rules.params.purchase_date_from = datetime(2026, 5, 4).date()
    revised.rules.version = "public_revised"
    content = text(reply("youth:rules", revised, store))
    assert "2026-05-04" in content and "public_revised" in content
    assert "目前不在有效期間" in text(reply("youth:rules", bundle, store, now=NOW + timedelta(days=150)))
    demo = load_bundle(Path(__file__).parents[1] / "app/data/precheck-demo.json")
    demo_content = text(reply("youth:rules", demo, store))
    assert "DEMO／合成規則摘要" in demo_content and "公開補助規則摘要" not in demo_content


@pytest.mark.parametrize("billing,channel,expected", [
    ("年費訂閱", "代購", ("annual", "subscription", "agent")),
    ("單獨額度／API", "集合平台", ("credits", "standalone_credits", "marketplace")),
    ("訂閱＋額外額度", "App Store／Play", ("other", "mixed", "app_store")),
    ("月費訂閱", "不確定通路", ("monthly", "subscription", "unsure")),
])
def test_public_choice_mapping_and_no_chat_eligibility_decision(bundle, store, billing, channel, expected):
    messages = flow(bundle, store, ("尚未購買", "其他／未列名工具", billing, channel))
    values = chat.read_handoff(handoff(messages), store=store, now=NOW)
    assert (values["billing_type"], values["billing_component"], values["purchase_channel"]) == expected
    assert values["tool_id"] is None and values["purchase_stage"] == "planning"
    assert "不在公告補助範圍" not in text(messages)
    assert "完整預檢尚未完成" in text(messages)
    assert "身分關係證明" not in text(messages) and "共同切結書" not in text(messages)


def test_catalogue_pagination_all_items_and_old_page_rejected(bundle, store):
    current = reply(choose(reply("預檢", bundle, store), "已經購買"), bundle, store)
    first_page = copy.deepcopy(current)
    seen = []
    while True:
        assert_limits(current)
        current = [message for message in current if message["type"] == "flex"]
        assert "其他／未列名工具" in [item["label"] for item in actions(current)]
        for action in actions(current):
            if action["type"] == "postback" and action["label"] not in {"其他／未列名工具", "上一頁工具", "下一頁工具"}:
                seen.append(action["displayText"])
        if "下一頁工具" not in [item["label"] for item in actions(current)]:
            break
        current = reply(choose(current, "下一頁工具"), bundle, store)
    assert seen == [tool.name for tool in bundle.tools]
    prior = chat.conversation_state(ACTOR, store=store, now=NOW)
    stale = reply(choose(first_page, "ChatGPT"), bundle, store)
    assert "舊按鈕" in text(stale)
    assert chat.conversation_state(ACTOR, store=store, now=NOW) == prior


def test_previous_step_cannot_overwrite_newer_state(bundle, store):
    initial = reply("預檢", bundle, store)
    token = choose(initial, "已經購買")
    current = reply(token, bundle, store)
    current = reply(choose(current, "ChatGPT"), bundle, store)
    before = chat.conversation_state(ACTOR, store=store, now=NOW)
    assert before["step"] == 2
    assert "舊按鈕" in text(reply(token, bundle, store))
    assert chat.conversation_state(ACTOR, store=store, now=NOW) == before


def test_new_start_and_navigation_invalidate_previous_buttons(bundle, store):
    initial = reply("預檢", bundle, store)
    token = choose(initial, "已經購買")
    reply("預檢", bundle, store)
    assert "舊按鈕" in text(reply(token, bundle, store))
    current = reply("預檢", bundle, store)
    token = choose(current, "已經購買")
    reply("選單", bundle, store)
    assert "舊按鈕" in text(reply(token, bundle, store))


def test_duplicate_webhook_event_returns_cached_reply_without_reapplying(bundle, store):
    initial = reply("預檢", bundle, store, event_key="event-start")
    same = reply("預檢", bundle, store, event_key="event-start")
    assert initial == same
    token = choose(initial, "已經購買")
    result = reply(token, bundle, store, event_key="event-stage")
    before = chat.conversation_state(ACTOR, store=store, now=NOW)
    assert reply(token, bundle, store, event_key="event-stage") == result
    assert chat.conversation_state(ACTOR, store=store, now=NOW) == before
    # Caller edits cannot mutate the cache.
    result[0]["text"] = "tampered"
    assert reply(token, bundle, store, event_key="event-stage")[0]["text"] != "tampered"


@pytest.mark.parametrize("changed", [{"actor_key": "other-user"}, {"secret_key": "different-secret"},
    {"now": NOW + timedelta(seconds=1200)}, {"now": NOW - timedelta(seconds=1)}])
def test_actor_secret_and_fixed_expiry_binding(bundle, store, changed):
    token = choose(reply("預檢", bundle, store), "已經購買")
    assert "已過期" in text(reply(token, bundle, store, **changed))


def test_expiry_does_not_slide_and_version_changes_invalidate(bundle, store):
    token = choose(reply("預檢", bundle, store), "已經購買")
    current = reply(token, bundle, store, now=NOW + timedelta(minutes=19))
    next_token = choose(current, "ChatGPT")
    assert chat._unpack(next_token)[0][0] == int(NOW.timestamp()) + 1200
    revised = bundle.model_copy(deep=True)
    revised.catalog_version = "updated"
    assert "版本已更新" in text(reply(next_token, revised, store))
    assert "已過期" in text(reply(next_token, bundle, store, now=NOW + timedelta(minutes=20)))


@pytest.mark.parametrize("data", ["youth:private-name", "youth:v2." + "x" * 500, "youth:v1.oldtoken.signature",
                                "youth:start&name=private", "youth:v2.invalid." + "A" * 43])
def test_malformed_postbacks_are_replaced_with_constant(data):
    assert chat.normalize_event_command({"type": "postback", "postback": {"data": data}}) == "youth:invalid"


@pytest.mark.parametrize("state", [
    [1789791600, "0" * 16, "0" * 24, 0, 0, "private-name"],
    [1789791600, "private-name", "0" * 24, 0, 0, 0],
    [1789791600, "0" * 16, "0" * 24, True, 0, 0],
    [1789791600, "0" * 16, "0" * 24, 0, 0, 0, "extra"],
])
def test_state_parser_rejects_hidden_personal_fields(state):
    payload = base64.urlsafe_b64encode(json.dumps(state, separators=(",", ":")).encode()).decode().rstrip("=")
    command = "youth:v2." + payload + "." + "A" * 43
    assert chat.normalize_event_command({"type": "postback", "postback": {"data": command}}) == "youth:invalid"


def test_handoff_expires_consumes_and_never_grants_identity(bundle, store):
    token = handoff(flow(bundle, store))
    first = chat.read_handoff(token, store=store, now=NOW)
    first["tool_id"] = "tamper"
    assert chat.read_handoff(token, store=store, now=NOW)["tool_id"] == "chatgpt"
    consumed = chat.consume_handoff(token, store=store, now=NOW)
    assert set(consumed) == {"purchase_stage", "tool_id", "billing_type", "billing_component", "purchase_channel"}
    assert chat.read_handoff(token, store=store, now=NOW) is None
    token = handoff(flow(bundle, store))
    assert chat.read_handoff(token, store=store, now=NOW + timedelta(minutes=20)) is None
    assert chat.read_handoff("forged-case-id", store=store, now=NOW) is None


def test_new_start_invalidates_old_handoff_and_result_write(bundle, store):
    token = handoff(flow(bundle, store))
    reply("預檢", bundle, store)
    assert chat.read_handoff(token, store=store, now=NOW) is None
    result = evaluate(PrecheckInput(), bundle, now=NOW)
    assert not chat.record_handoff_result(token, result, store=store, now=NOW)


def test_real_backend_result_is_reduced_to_safe_aggregate_only(bundle, store):
    token = handoff(flow(bundle, store))
    result = evaluate(PrecheckInput(purchase_stage="purchased", tool_id="chatgpt", application_type="specific",
        qualification_categories=["low_income"], birth_date="2000-01-01", residency="hsinchu",
        payer="other", payer_relationship="parent", eligible_cost_twd="9876", plan_name="private-plan"),
        bundle, now=NOW)
    assert chat.record_handoff_result(token, result, store=store, now=NOW)
    safe = chat.conversation_state(ACTOR, store=store, now=NOW)["result"]
    assert set(safe) == {"summary", "rules_version", "mode", "outcome", "saved"}
    assert safe["saved"] is False
    assert set(safe["summary"]) == {"required_total", "completed", "incomplete", "issues"}
    serialized = json.dumps(safe)
    for forbidden in ("low_income", "9876", "private-plan", "2000-01-01", "parent", "documents", "checks", "case_id"):
        assert forbidden not in serialized
    messages = reply("youth:result", bundle, store)
    assert "預檢尚未完成" in text(messages)
    assert "個人" not in text(messages)  # Counts and generic login entry only.
    assert "9876" not in text(messages) and "身分" not in text(messages)
    assert_limits(messages)


@pytest.mark.parametrize("outcome,summary,checks,title", [
    ("no_issue", {"required_total": 3, "completed": 3, "incomplete": 0, "issues": 0}, [], "已執行檢查未發現異常"),
    ("action_needed", {"required_total": 3, "completed": 3, "incomplete": 0, "issues": 1},
     [{"required": True, "outcome": "action_needed", "reason": "private-doc-issue"}], "有待處理事項"),
    ("manual_review", {"required_total": 3, "completed": 3, "incomplete": 0, "issues": 1},
     [{"required": True, "outcome": "manual_review", "reason": "private-qualification"}], "需要人工確認"),
    ("incomplete", {"required_total": 3, "completed": 2, "incomplete": 1, "issues": 0}, [], "預檢尚未完成"),
])
def test_result_statuses_and_three_safe_visual_variants(bundle, store, outcome, summary, checks, title):
    token = handoff(flow(bundle, store))
    result = {"summary": summary, "checks": checks, "rules_version": bundle.rules.version, "mode": "public_advisory"}
    assert chat.record_handoff_result(token, result, store=store, now=NOW)
    state = chat.conversation_state(ACTOR, store=store, now=NOW)
    assert state["result"]["outcome"] == outcome
    messages = reply("_result", bundle, store)
    assert title in text(messages) and "補正已受理" in text(messages)
    assert "private-" not in text(messages)
    assert_limits(messages)


def test_files_progress_correction_are_generic_login_entries(bundle, store):
    for command in ("文件", "進度", "補正"):
        messages = reply(command, bundle, store)
        entry_action = {"文件": "apply", "進度": "tracking", "補正": "supplement"}[command]
        assert any(action.get("uri") == f"http://127.0.0.1:8765/?entry=line&action={entry_action}"
                   for action in actions(messages))
        assert "登入" in text(messages)
        assert "共同切結書" not in text(messages)
        assert "卡號末四碼" not in text(messages)
    assert "不代表補正已提交或已受理" in text(reply("補正", bundle, store))
    assert "貼上的案件網址授權" in text(reply("進度", bundle, store))


def test_unsaved_result_returns_to_full_precheck_instead_of_unrelated_case_tracking(bundle, store):
    messages = flow(bundle, store)
    handoff_uri = next(action["uri"] for action in actions(messages) if "line_handoff=" in action.get("uri", ""))
    assert urlsplit(handoff_uri).path == "/precheck"
    assert set(parse_qs(urlsplit(handoff_uri).query)) == {"line_handoff"}
    result_messages = reply("youth:result", bundle, store)
    assert any(action.get("uri") == handoff_uri for action in actions(result_messages))
    assert "尚未保存" in text(result_messages) and "需重新填答" in text(result_messages)
    assert "line_result" not in json.dumps(result_messages)


def test_authenticated_saved_result_routes_to_opaque_authorized_lookup(bundle, store):
    token = handoff(flow(bundle, store))
    result = {**success_result(bundle), "case_id": "private-case", "owner_id": "private-owner"}
    assert chat.record_handoff_result(token, result, saved=True, store=store, now=NOW)
    safe = chat.conversation_state(ACTOR, store=store, now=NOW)["result"]
    assert safe["saved"] is True
    assert set(safe) == {"summary", "rules_version", "mode", "outcome", "saved"}
    messages = reply("youth:result", bundle, store,
                     public_url=WEB + "?caseId=private-case&name=private-name#private-token")
    url = next(action["uri"] for action in actions(messages) if action["type"] == "uri")
    parsed = urlsplit(url)
    assert parsed.path == "/"
    assert parse_qs(parsed.query) == {"entry": ["line"], "action": ["tracking"], "line_result": [token]}
    assert not parsed.fragment and "private-" not in json.dumps(messages)
    assert "已保存預檢（未正式送件）" in text(messages)
    assert_limits(messages)


def test_result_payload_cannot_claim_saved_and_new_evaluation_clears_old_save_status(bundle, store):
    token = handoff(flow(bundle, store))
    result = {**success_result(bundle), "saved": True, "verified": True, "case_id": "private-case"}
    assert chat.record_handoff_result(token, result, store=store, now=NOW)
    assert chat.conversation_state(ACTOR, store=store, now=NOW)["result"]["saved"] is False
    assert "line_result" not in json.dumps(reply("youth:result", bundle, store))
    assert chat.record_handoff_result(token, result, saved=True, store=store, now=NOW)
    assert "line_result" in json.dumps(reply("youth:result", bundle, store, event_key="saved-result"))
    assert chat.record_handoff_result(token, result, store=store, now=NOW)
    messages = reply("youth:result", bundle, store, event_key="saved-result")
    assert "尚未保存" in text(messages) and "line_result" not in json.dumps(messages)


@pytest.mark.parametrize("change", ["expire", "restart", "version"])
def test_saved_result_link_does_not_survive_expiry_restart_or_rule_change(bundle, store, change):
    token = handoff(flow(bundle, store))
    assert chat.record_handoff_result(token, success_result(bundle), saved=True, store=store, now=NOW)
    assert "line_result" in json.dumps(reply("youth:result", bundle, store))
    options = {}
    if change == "expire":
        options["now"] = NOW + timedelta(minutes=20)
    elif change == "restart":
        reply("預檢", bundle, store)
    else:
        bundle = bundle.model_copy(deep=True)
        bundle.catalog_version = "revised"
    messages = reply("youth:result", bundle, store, **options)
    assert "line_result" not in json.dumps(messages)
    assert "已保存預檢（未正式送件）" not in text(messages)


def test_saved_result_link_keeps_liff_path_but_requires_a_valid_opaque_token():
    token = "A" * 32
    configured = "https://liff.line.me/1234567890-example/app?caseId=private-case#private-token"
    parsed = urlsplit(chat._saved_result_url(configured, token))
    assert parsed.path == "/1234567890-example/app"
    assert parse_qs(parsed.query) == {"entry": ["line"], "action": ["tracking"], "line_result": [token]}
    assert not parsed.fragment
    assert chat._saved_result_url(configured, "private-case") is None
    assert chat._saved_result_url(None, token) is None


def test_unsaved_result_handoff_never_copies_configured_private_query(bundle, store):
    token = handoff(flow(bundle, store))
    assert chat.record_handoff_result(token, success_result(bundle), store=store, now=NOW)
    messages = reply("youth:result", bundle, store, public_url=WEB + "?name=private-person#private-token")
    assert "private-" not in json.dumps(messages)
    assert parse_qs(urlsplit(next(action["uri"] for action in actions(messages) if action["type"] == "uri")).query) == {
        "line_handoff": [token],
    }


@pytest.mark.parametrize("action", ["tracking", "supplement", "apply", "safety"])
def test_login_entry_keeps_liff_app_path_and_never_copies_private_query_or_fragment(action):
    configured = "https://liff.line.me/1234567890-example/app?caseId=private-case&name=private-person#line_handoff=private"
    uri = chat.login_entry_url(configured, action)
    parsed = urlsplit(uri)
    assert parsed.scheme == "https" and parsed.hostname == "liff.line.me"
    assert parsed.path == "/1234567890-example/app"
    assert parse_qs(parsed.query) == {"entry": ["line"], "action": [action]}
    assert not parsed.fragment and "private" not in uri and "caseId" not in uri
    assert chat.login_entry_url(WEB, "case/123") is None


@pytest.mark.parametrize("purpose", ["辨識可疑補助訊息", "保護個人資料"])
@pytest.mark.parametrize("answer", ["另由官方入口確認", "直接照訊息要求操作"])
def test_safety_purpose_quiz_is_separate_from_eligibility(bundle, store, purpose, answer):
    first = reply("安全", bundle, store)
    assert "不影響補助" in text(first)
    quiz = reply(choose(first, purpose), bundle, store)
    old = choose(quiz, answer)
    feedback = reply(old, bundle, store)
    assert "不影響補助結果" in text(feedback)
    assert "舊按鈕" in text(reply(old, bundle, store))
    assert chat.conversation_state(ACTOR, store=store, now=NOW)["result"] is None


def test_assistance_uses_source_without_fake_human_support(bundle, store):
    rendered = text(reply("協助", bundle, store))
    assert "沒有串接即時人工客服" in rendered
    assert bundle.rules.source.url in rendered
    assert bundle.rules.version in rendered


def test_safety_answers_never_modify_an_existing_precheck_result(bundle, store):
    token = handoff(flow(bundle, store))
    result = evaluate(PrecheckInput(), bundle, now=NOW)
    assert chat.record_handoff_result(token, result, store=store, now=NOW)
    original = chat.conversation_state(ACTOR, store=store, now=NOW)["result"]
    current = reply("安全", bundle, store)
    current = reply(choose(current, "辨識可疑補助訊息"), bundle, store)
    reply(choose(current, "直接照訊息要求操作"), bundle, store)
    assert chat.conversation_state(ACTOR, store=store, now=NOW)["result"] == original


def success_result(bundle):
    return {"summary": {"required_total": 3, "completed": 3, "incomplete": 0, "issues": 0},
            "checks": [], "rules_version": bundle.rules.version, "mode": "public_advisory"}


def test_safety_during_pending_handoff_preserves_prefill_and_accepts_web_result(bundle, store):
    token = handoff(flow(bundle, store))
    before = chat.read_handoff(token, store=store, now=NOW)
    session_before = copy.deepcopy(store.sessions[ACTOR])
    current = reply("安全", bundle, store)
    quiz = reply(choose(current, "保護個人資料"), bundle, store)
    assert store.sessions[ACTOR] == session_before
    assert chat.read_handoff(token, store=store, now=NOW) == before
    assert chat.record_handoff_result(token, success_result(bundle), store=store, now=NOW)
    feedback = reply(choose(quiz, "直接照訊息要求操作"), bundle, store)
    assert "不影響補助結果" in text(feedback)
    assert chat.conversation_state(ACTOR, store=store, now=NOW)["result"]["outcome"] == "no_issue"
    assert "已執行檢查未發現異常" in text(reply("youth:result", bundle, store))


def test_safety_during_partial_chat_does_not_clear_or_advance_precheck_state(bundle, store):
    step = reply(choose(reply("預檢", bundle, store), "已經購買"), bundle, store)
    before = copy.deepcopy(store.sessions[ACTOR])
    quiz = reply("安全", bundle, store)
    quiz = reply(choose(quiz, "辨識可疑補助訊息"), bundle, store)
    reply(choose(quiz, "另由官方入口確認"), bundle, store)
    assert store.sessions[ACTOR] == before
    next_step = reply(choose(step, "ChatGPT"), bundle, store)
    assert "第 3／4 步" in text(next_step)


@pytest.mark.parametrize("use_handoff", [True, False])
def test_failure_replaces_prior_success_and_invalidates_cached_result_card(bundle, store, use_handoff):
    token = handoff(flow(bundle, store))
    assert chat.record_handoff_result(token, success_result(bundle), store=store, now=NOW)
    successful = reply("youth:result", bundle, store, event_key="result-before-failure")
    assert "已執行檢查未發現異常" in text(successful)
    recorded = (chat.record_handoff_failure(token, store=store, now=NOW) if use_handoff else
                chat.record_actor_failure(ACTOR, store=store, now=NOW))
    assert recorded
    assert chat.conversation_state(ACTOR, store=store, now=NOW)["result"] == {"outcome": "failed"}
    for event_key in ("result-before-failure", "result-after-failure"):
        messages = reply("youth:result", bundle, store, event_key=event_key)
        assert "本次預檢未完成" in text(messages)
        assert "已執行檢查未發現異常" not in text(messages)
        assert "已完成 3 項" not in text(messages)
        assert_limits(messages)


def test_failure_helpers_reject_absent_expired_or_superseded_context(bundle, store):
    assert not chat.record_actor_failure(ACTOR, store=store, now=NOW)
    token = handoff(flow(bundle, store))
    reply("預檢", bundle, store)
    assert not chat.record_handoff_failure(token, store=store, now=NOW)
    assert not chat.record_handoff_failure("forged", store=store, now=NOW)
    assert not chat.record_actor_failure(ACTOR, store=store, now=NOW + timedelta(minutes=20))


@pytest.mark.parametrize("same_event", [True, False])
@pytest.mark.parametrize("change_mode", [True, False])
def test_result_view_rejects_changed_bundle_including_cached_reply(bundle, store, same_event, change_mode):
    token = handoff(flow(bundle, store))
    assert chat.record_handoff_result(token, success_result(bundle), store=store, now=NOW)
    assert "已執行檢查未發現異常" in text(reply("youth:result", bundle, store, event_key="version-view"))
    if change_mode:
        revised = load_bundle(Path(__file__).parents[1] / "app/data/precheck-demo.json")
    else:
        revised = bundle.model_copy(deep=True)
        revised.catalog_version = "revised"
    messages = reply("youth:result", revised, store, event_key="version-view" if same_event else "new-view")
    assert "規則版本已更新" in text(messages)
    assert "已執行檢查未發現異常" not in text(messages)


def test_failure_scope_distinguishes_signed_precheck_from_safety_practice(bundle, store):
    assert chat.is_precheck_command("預檢") and chat.is_precheck_command("youth:result")
    assert not chat.is_precheck_command("安全") and not chat.is_precheck_command("協助")
    precheck = choose(reply("預檢", bundle, store), "已經購買")
    safety = choose(reply("安全", bundle, store), "保護個人資料")
    assert chat.is_precheck_command(precheck)
    assert not chat.is_precheck_command(safety)
    assert not chat.is_precheck_command("youth:invalid")


def test_backend_gallery_helper_labels_demo_and_never_mutates_live_state(bundle, store):
    flow(bundle, store)
    before = copy.deepcopy(store.sessions)
    demo = load_bundle(Path(__file__).parents[1] / "app/data/precheck-demo.json")
    result = evaluate(PrecheckInput(), demo, now=NOW)
    messages = chat.result_reply_from_backend(result, public_url=WEB)
    assert "DEMO／合成規則" in text(messages)
    assert "非目前申請結果" in text(messages)
    assert store.sessions == before
    assert_limits(messages)
    public = chat.result_reply_from_backend(evaluate(PrecheckInput(), bundle, now=NOW), public_url=WEB)
    assert "DEMO／合成規則" not in text(public)


@pytest.mark.parametrize("result", [None, {}, {"summary": {}}, {"summary": {"required_total": "private-data"}}])
def test_backend_gallery_helper_fails_safely_for_malformed_result(result):
    messages = chat.result_reply_from_backend(result)
    assert "暫時無法完成" in text(messages)
    assert "private-data" not in text(messages)


@pytest.mark.parametrize("url", [None, "http://external.example/precheck", "https://user:secret@example.com",
    "https://example.com/\nprivate", "https://example.com:bad/", "javascript:alert(1)"])
def test_unconfigured_or_unsafe_url_is_never_used(bundle, store, url):
    messages = reply("文件", bundle, store, public_url=url)
    assert not any(action["type"] == "uri" for action in actions(messages))
    assert "尚未設定" in text(messages)


def test_missing_identity_failure_and_unknown_are_safe(bundle, store):
    assert "暫時無法完成" in text(reply("預檢", bundle, store, actor_key=""))
    assert "暫時無法完成" in text(reply("預檢", bundle, store, secret_key=""))
    assert "自由文字" in text(reply("隱私提醒", bundle, store))
    assert "未代為送件" in text(chat.failure_reply())


def test_store_is_bounded_purges_on_expiry_and_clears(bundle):
    bounded = chat.ConversationStore(max_sessions=2, max_handoffs=2, max_events=2)
    for index in range(5):
        reply("預檢", bundle, bounded, actor_key=f"actor-{index}", event_key=f"event-{index}")
    assert len(bounded.sessions) == len(bounded.events) == 2
    chat.conversation_state("actor-4", store=bounded, now=NOW + timedelta(minutes=20))
    assert not bounded.sessions and not bounded.events
    flow(bundle, bounded)
    reply("安全", bundle, bounded)
    assert bounded.handoffs
    assert bounded.safety_sessions
    bounded.clear()
    assert not bounded.sessions and not bounded.events and not bounded.handoffs and not bounded.safety_sessions


def test_maximum_catalogue_and_non_bmp_names_stay_in_line_limits(bundle, store):
    expanded = bundle.model_copy(deep=True)
    template = expanded.tools[0]
    expanded.tools = [template.model_copy(update={"id": f"test-{index}", "name": "🤖" * 200}) for index in range(100)]
    menus = reply(choose(reply("預檢", expanded, store), "已經購買"), expanded, store)
    pages = 0
    while True:
        assert_limits(menus)
        pages += 1
        if "下一頁工具" not in [action["label"] for action in actions(menus)]:
            break
        menus = reply(choose(menus, "下一頁工具"), expanded, store)
    assert pages == 12

