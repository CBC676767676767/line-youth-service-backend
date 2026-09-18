"""OTP delivery. Development messages are written only to a private local spool."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import os
from pathlib import Path
import smtplib
import ssl
import stat
from uuid import uuid4


class MailDeliveryError(RuntimeError):
    """Delivery failed without exposing SMTP credentials or message contents."""


@dataclass(frozen=True)
class MailConfig:
    backend: str
    sender: str
    spool_dir: Path | None = None
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = field(default=None, repr=False)
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    production: bool = False


def _private_spool(directory: Path, payload: bytes) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise MailDeliveryError("Mail spool must be an owned private directory")
    if stat.S_IMODE(info.st_mode) != 0o700:
        directory.chmod(0o700)
    path = directory / f"{uuid4().hex}.eml"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def send_otp(config: MailConfig, recipient: str, code: str, *, expires_minutes: int) -> None:
    """Send one code; callers must never return the code or spool path to a client."""
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = recipient
    message["Subject"] = "青年申辦服務：電子信箱驗證碼"
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid()
    message.set_content(
        f"您的電子信箱驗證碼：{code}\n\n"
        f"驗證碼於 {expires_minutes} 分鐘後失效，僅供本次操作使用。\n"
        "請勿將驗證碼提供給其他人。若您未提出此操作，請忽略此信。\n"
        "信箱驗證只證明信箱控制，不代表實名或申請資格已通過。\n"
    )
    try:
        if config.backend == "spool":
            if config.production or config.spool_dir is None:
                raise MailDeliveryError("Private mail spool is development-only")
            _private_spool(config.spool_dir, message.as_bytes())
            return
        if config.backend != "smtp" or not config.smtp_host:
            raise MailDeliveryError("Mail transport is not configured")
        if not config.smtp_ssl and not config.smtp_starttls:
            raise MailDeliveryError("SMTP requires TLS")
        tls = ssl.create_default_context()
        if config.smtp_ssl:
            transport = smtplib.SMTP_SSL(
                config.smtp_host, config.smtp_port, timeout=10, context=tls
            )
        else:
            transport = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=10)
        with transport:
            transport.ehlo()
            if not config.smtp_ssl:
                transport.starttls(context=tls)
                transport.ehlo()
            if config.smtp_username:
                if not config.smtp_password:
                    raise MailDeliveryError("SMTP authentication is not configured")
                transport.login(config.smtp_username, config.smtp_password)
            if transport.send_message(message):
                raise MailDeliveryError("Recipient was refused")
    except MailDeliveryError:
        raise
    except (OSError, smtplib.SMTPException, ValueError) as exc:
        raise MailDeliveryError("Mail delivery is temporarily unavailable") from exc
