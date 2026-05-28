from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any

from yutome.hosted.email import EmailMessage, RESEND_USER_AGENT, ResendEmailSender


class _FakeResponse(AbstractContextManager["_FakeResponse"]):
    status = 200

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


def test_resend_sender_sets_json_and_product_headers(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request, *, timeout: float):  # noqa: ANN001
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    sender = ResendEmailSender(api_key="re_test", sender="Yutome <noreply@getyutome.com>", timeout_seconds=3)

    sender.send(EmailMessage(to="alice@example.com", subject="Sign in", text="Use this link"))

    assert captured["timeout"] == 3
    assert captured["headers"]["Authorization"] == "Bearer re_test"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-agent"] == RESEND_USER_AGENT
