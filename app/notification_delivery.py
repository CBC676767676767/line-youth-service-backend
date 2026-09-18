"""Notification delivery adapters and immutable encrypted request snapshots."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

import httpx
from cryptography.fernet import Fernet, InvalidToken


ACTIONABLE_PURPOSES = {
    "TASK_CREATED", "TASK_REVISED", "TASK_REOPENED", "TASK_REMINDER",
    "REMINDER", "SUPPLEMENT_REQUIRED",
}
PURPOSE_LABELS = {
    "CASE_SUBMITTED": "申請已收件，查看回執與下一步",
    "TASK_CREATED": "有待處理事項，請查看內容與期限",
    "TASK_REVISED": "待辦內容已更新，請查看最新要求與期限",
    "TASK_REOPENED": "需要再次補正，請查看本次要求",
    "TASK_REMINDER": "待辦期限提醒，請查看並完成操作",
    "TASK_SUBMITTED": "補件已收件，等待承辦確認",
    "TASK_ACCEPTED": "補件已確認，查看案件進度",
    "TASK_CANCELLED": "待辦已取消，查看最新案件進度",
    "CASE_DECIDED": "案件已有處理結果，請登入查看",
    "CASE_CORRECTED": "案件結果已有更新，請登入查看",
    "CASE_CLOSED": "案件已結案，請登入查看",
    "CASE_WITHDRAWN": "案件撤回已完成，請登入查看",
}


def _cipher(secret_key: str) -> Fernet:
    key = hmac.new(secret_key.encode(), b"youth-service:notification-snapshot:v1", hashlib.sha256).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def encrypt_snapshot(payload: dict, secret_key: str) -> dict:
    return {"format": "fernet-v1", "encrypted": _cipher(secret_key).encrypt(canonical(payload).encode()).decode()}


def decrypt_snapshot(payload: dict, secret_key: str) -> dict:
    if payload.get("format") != "fernet-v1":
        raise ValueError("Notification payload has not been securely frozen")
    try:
        result = json.loads(_cipher(secret_key).decrypt(payload["encrypted"].encode()))
    except (InvalidToken, KeyError, ValueError, TypeError) as exc:
        raise ValueError("Notification snapshot cannot be decrypted") from exc
    if not isinstance(result, dict):
        raise ValueError("Invalid notification snapshot")
    return result


def line_payload(intent, recipient: str, settings) -> dict:
    title = PURPOSE_LABELS.get(intent.purpose, "案件服務有更新，請登入查看")
    path = f"/tasks/{intent.task_id}" if intent.task_id else f"/cases/{intent.case_id}"
    return {
        "to": recipient,
        "messages": [{
            "type": "flex",
            "altText": title,
            "contents": {
                "type": "bubble",
                "body": {"type": "box", "layout": "vertical", "contents": [
                    {"type": "text", "text": title, "wrap": True, "weight": "bold"},
                    {"type": "text", "text": "登入官方服務查看最新狀態、操作內容及期限。", "wrap": True, "size": "sm"},
                ]},
                "footer": {"type": "box", "layout": "vertical", "contents": [
                    {"type": "button", "action": {"type": "uri", "label": "查看並處理", "uri": str(settings.public_origin).rstrip("/") + path}},
                ]},
            },
        }],
    }


@dataclass(frozen=True)
class DeliveryResult:
    result: str
    http_status: int | None = None
    provider_request_id: str | None = None
    accepted_request_id: str | None = None
    error_code: str | None = None
    retryable: bool = False
    uncertain: bool = False


def send_line(payload: dict, retry_key: str, settings, *, transport=None) -> DeliveryResult:
    """One attempt only; the persistent worker owns retries and all state."""
    if not settings.line_channel_access_token:
        return DeliveryResult("FAILED", error_code="LINE_NOT_CONFIGURED")
    try:
        with httpx.Client(timeout=15.0, transport=transport, follow_redirects=False) as client:
            response = client.post(
                "https://api.line.me/v2/bot/message/push",
                headers={
                    "Authorization": "Bearer " + settings.line_channel_access_token,
                    "X-Line-Retry-Key": retry_key,
                    "Content-Type": "application/json",
                },
                content=canonical(payload).encode("utf-8"),
            )
    except (httpx.TimeoutException, httpx.NetworkError):
        return DeliveryResult("UNKNOWN", error_code="LINE_NETWORK_UNKNOWN", retryable=True, uncertain=True)
    except httpx.HTTPError:
        return DeliveryResult("UNKNOWN", error_code="LINE_TRANSPORT_UNKNOWN", retryable=True, uncertain=True)
    request_id = response.headers.get("x-line-request-id")
    accepted_id = response.headers.get("x-line-accepted-request-id")
    if 200 <= response.status_code < 300 or (response.status_code == 409 and accepted_id):
        return DeliveryResult("API_ACCEPTED", response.status_code, request_id, accepted_id)
    if response.status_code >= 500:
        return DeliveryResult("UNKNOWN", response.status_code, request_id, error_code="LINE_SERVER_UNKNOWN", retryable=True, uncertain=True)
    # Official retry guidance does not treat arbitrary 4xx as retryable. A 409
    # without the accepted-request header is not evidence of prior acceptance.
    return DeliveryResult("FAILED", response.status_code, request_id, error_code=f"LINE_HTTP_{response.status_code}")


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret or not signature or len(signature) > 128:
        return False
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode("ascii")
    try:
        return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))
    except UnicodeEncodeError:
        return False
