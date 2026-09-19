"""Synthetic HTTP conversation -> web precheck -> safe card, without LINE traffic."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.main import create_app
from app.models import Case, WebhookInbox
from app.precheck_models import PrecheckSnapshot


@pytest.fixture
def simulation(tmp_path):
    settings = Settings(_env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path / 'simulation.db'}",
                        line_simulator_enabled=True,
                        secret_key="local-simulator-testing-" * 3,
                        totp_encryption_key=Fernet.generate_key().decode(),
                        public_origin="http://127.0.0.1:8765", allowed_origins=["http://testserver"],
                        storage_dir=tmp_path / "files", mail_spool_dir=tmp_path / "mail")
    app = create_app(settings)
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client, app


def actions(value):
    if isinstance(value, dict):
        if value.get("type") in {"postback", "uri"} and "label" in value:
            yield value
        for child in value.values():
            yield from actions(child)
    elif isinstance(value, list):
        for child in value:
            yield from actions(child)


def start(client):
    response = client.post("/api/v1/line/simulator/sessions")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["delivery"] == "fake" and len(data["rich_menu"]["areas"]) == 6
    return data["session_id"], data


def emit(client, session_id, *, text=None, data=None, **extra):
    body = {"session_id": session_id, "kind": "postback" if data is not None else "text"}
    if data is not None:
        body["data"] = data
    if text is not None:
        body["text"] = text
    body.update(extra)
    response = client.post("/api/v1/line/simulator/events", json=body)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def choose(client, session_id, response, label):
    action = next(action for action in actions(response["messages"]) if label in action["label"])
    return emit(client, session_id, data=action["data"])


def to_handoff(client, session_id, purchased=True, unknown=False):
    response = emit(client, session_id, text="預檢")
    response = choose(client, session_id, response, "已經購買" if purchased else "未購買")
    response = choose(client, session_id, response, "未列名" if unknown else "ChatGPT")
    response = choose(client, session_id, response, "月")
    response = choose(client, session_id, response, "官方")
    uri = next(action["uri"] for action in actions(response["messages"]) if "line_handoff=" in action.get("uri", ""))
    token = parse_qs(urlsplit(uri).query)["line_handoff"][0]
    return token, response


@pytest.mark.parametrize("purchased,unknown", [(True, False), (False, False), (True, True)])
def test_chat_handoff_precheck_safe_result_and_no_case(simulation, purchased, unknown):
    client, app = simulation
    sid, _ = start(client)
    token, _ = to_handoff(client, sid, purchased, unknown)
    response = client.get("/api/v1/line/handoffs/" + token)
    assert response.status_code == 200
    choices = response.json()["data"]["choices"]
    assert choices["purchase_stage"] == ("purchased" if purchased else "planning")
    assert (choices["tool_id"] is None) == unknown
    assert set(choices) == {"purchase_stage", "tool_id", "billing_type", "billing_component", "purchase_channel"}
    form = {**choices, "tool_name": "測試未列工具" if unknown else "ChatGPT", "plan_name": "合成訂閱",
            "residency": "hsinchu", "birth_date": "2000-01-01", "application_type": "specific",
            "qualification_categories": ["low_income"], "eligible_cost_twd": "4000",
            "purchase_date": "2026-09-01", "subscription_start": "2026-09-01",
            "subscription_end": "2026-10-01", "payer": "self", "payment_method": "credit_card"}
    evaluation = client.post("/api/v1/precheck/evaluate", json=form, headers={"X-Line-Handoff": token})
    assert evaluation.status_code == 200, evaluation.text
    card = emit(client, sid, data="youth:result")
    rendered = json.dumps(card["messages"], ensure_ascii=False)
    for private in ("2000-01-01", "低收入戶", "3600", "4000", "身分證正反面"):
        assert private not in rendered
    assert "public_2026_09_19" in rendered
    with app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Case)) == 0
        assert db.scalar(select(func.count()).select_from(PrecheckSnapshot)) == 0
        assert all(row.payload == {} for row in db.scalars(select(WebhookInbox)))
    # A handoff cannot replace existing login, CSRF, or case authorization.
    assert client.get("/api/v1/cases/other-person-case/precheck", headers={"X-Line-Handoff": token}).status_code == 401
    assert client.post("/api/v1/cases/other-person-case/precheck", json=form,
                       headers={"X-Line-Handoff": token}).status_code == 401


def test_repeat_and_old_button_do_not_advance(simulation):
    client, _ = simulation
    sid, _ = start(client)
    first = emit(client, sid, text="預檢")
    old = next(action["data"] for action in actions(first["messages"]) if action["type"] == "postback" and "已經購買" in action["label"])
    second = emit(client, sid, data=old)
    replay = emit(client, sid, repeat_last=True)
    assert replay["duplicate"] and replay["messages"] == [] and replay["state"] == second["state"]
    stale = emit(client, sid, data=old)
    assert stale["state"]["revision"] == second["state"]["revision"]
    assert any(word in json.dumps(stale["messages"], ensure_ascii=False) for word in ("舊", "較早", "失效"))


def test_expired_button_and_handoff_require_restart(simulation):
    client, _ = simulation
    sid, _ = start(client)
    first = emit(client, sid, text="預檢")
    old = next(action["data"] for action in actions(first["messages"]) if "已經購買" in action["label"])
    client.post("/api/v1/line/simulator/advance", json={"session_id": sid, "minutes": 25})
    expired = emit(client, sid, data=old)
    assert "過期" in json.dumps(expired["messages"], ensure_ascii=False)
    token, _ = to_handoff(client, sid)
    client.post("/api/v1/line/simulator/advance", json={"session_id": sid, "minutes": 25})
    assert client.get("/api/v1/line/handoffs/" + token).status_code == 410


def test_invalid_input_attachment_failure_and_correction_are_honest(simulation):
    client, _ = simulation
    sid, _ = start(client)
    unknown = emit(client, sid, text="synthetic-private-name-bank-number")
    assert "synthetic-private" not in json.dumps(unknown["messages"])
    attachment = emit(client, sid, kind="attachment")
    assert "附件" in json.dumps(attachment["messages"], ensure_ascii=False)
    failure = emit(client, sid, kind="failure", text="預檢")
    assert failure["counts"]["failed"] == 1 and failure["messages"]
    assert "missing-simulator" not in json.dumps(failure)
    supplement = emit(client, sid, text="補正")
    rendered = json.dumps(supplement["messages"], ensure_ascii=False)
    assert "登入" in rendered and "已接受" not in rendered


def test_simulator_cannot_become_a_production_or_remote_interface(simulation):
    client, app = simulation
    assert client.post("/api/v1/line/simulator/sessions", headers={"Origin": "https://untrusted.example"}).status_code == 403
    app.state.settings.line_simulator_enabled = False
    assert client.get("/line-simulator").status_code == 404
    app.state.settings.line_simulator_enabled = True
    app.state.settings.app_env = "production"
    assert client.post("/api/v1/line/simulator/sessions").status_code == 404


def test_precheck_failure_never_records_a_success_card(simulation, monkeypatch):
    from app import precheck
    from app.common import ApiError
    client, _ = simulation
    sid, _ = start(client)
    token, _ = to_handoff(client, sid)

    def unavailable(_request):
        raise ApiError(503, "PRECHECK_RULES_UNAVAILABLE", "預檢暫時無法完成。")

    monkeypatch.setattr(precheck, "_configuration", unavailable)
    assert client.post("/api/v1/precheck/evaluate", json={}, headers={"X-Line-Handoff": token}).status_code == 503
    result = emit(client, sid, data="youth:result")
    rendered = json.dumps(result["messages"], ensure_ascii=False)
    assert "未" in rendered and "已完成" not in rendered


def test_advancing_one_actor_does_not_expire_another(simulation):
    client, _ = simulation
    actor_a, _ = start(client)
    actor_b, _ = start(client)
    token_b, _ = to_handoff(client, actor_b)
    actor_b_before = emit(client, actor_b, data="youth:result")["state"]
    client.post("/api/v1/line/simulator/advance", json={"session_id": actor_a, "minutes": 25})
    emit(client, actor_a, text="預檢")
    assert client.get("/api/v1/line/handoffs/" + token_b).status_code == 200
    assert emit(client, actor_b, data="youth:result")["state"] == actor_b_before
