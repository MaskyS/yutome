"""Minimal transactional email for hosted sign-in links.

The hosted control plane has no built-in mailer, so this provides a small
pluggable sender: ``ResendEmailSender`` for production (Resend HTTP API, no extra
dependency — stdlib ``urllib``) and ``LoggingEmailSender`` as the dev/no-config
fallback that records the message instead of delivering it. ``build_email_sender_from_env``
picks Resend when ``RESEND_API_KEY`` and ``YUTOME_EMAIL_FROM`` are set, else logging.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger("yutome.hosted.email")

RESEND_API_KEY_ENV_VAR = "RESEND_API_KEY"
EMAIL_FROM_ENV_VAR = "YUTOME_EMAIL_FROM"
RESEND_ENDPOINT = "https://api.resend.com/emails"


class EmailSendError(RuntimeError):
    """Raised when an email could not be handed off to the delivery provider."""


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    html: str | None = None


@runtime_checkable
class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class LoggingEmailSender:
    """Records the email instead of delivering it (local dev / unconfigured prod).

    Logs only the recipient and subject at INFO; the body (which contains the
    sign-in link) is logged at DEBUG so links do not land in default prod logs.
    """

    def send(self, message: EmailMessage) -> None:
        logger.info("email not delivered (no provider configured): to=%s subject=%s", message.to, message.subject)
        logger.debug("email body for %s:\n%s", message.to, message.text)


class ResendEmailSender:
    def __init__(self, *, api_key: str, sender: str, timeout_seconds: float = 15.0) -> None:
        self._api_key = api_key
        self._sender = sender
        self._timeout = timeout_seconds

    def send(self, message: EmailMessage) -> None:
        payload: dict[str, object] = {
            "from": self._sender,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html
        request = urllib.request.Request(
            RESEND_ENDPOINT,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                if status >= 300:
                    raise EmailSendError("Email provider request failed.")
        except EmailSendError:
            raise
        except Exception as exc:  # network/HTTP errors; do not leak provider detail upstream
            raise EmailSendError("Email provider request failed.") from exc


def build_email_sender_from_env(environ: Mapping[str, str] | None = None) -> EmailSender:
    env = os.environ if environ is None else environ
    api_key = (env.get(RESEND_API_KEY_ENV_VAR) or "").strip()
    sender = (env.get(EMAIL_FROM_ENV_VAR) or "").strip()
    if api_key and sender:
        return ResendEmailSender(api_key=api_key, sender=sender)
    return LoggingEmailSender()


__all__ = [
    "EMAIL_FROM_ENV_VAR",
    "RESEND_API_KEY_ENV_VAR",
    "EmailMessage",
    "EmailSendError",
    "EmailSender",
    "LoggingEmailSender",
    "ResendEmailSender",
    "build_email_sender_from_env",
]
