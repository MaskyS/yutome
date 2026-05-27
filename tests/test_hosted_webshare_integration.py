from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from yutome.config import ProxyConfig
from yutome.hosted.gate import UsageGate
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    UsageEvent,
    WorkspaceBalance,
)
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied
from yutome.youtube import fetch_transcript, fetch_video_metadata, proxy_url_for_ytdlp


class RecordingLedger:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    def append(self, event: UsageEvent) -> None:
        self.events.append(event)


def _webshare_proxy() -> ProxyConfig:
    return ProxyConfig(
        enabled=True,
        kind="webshare",
        webshare_username="proxy-user",
        webshare_password="proxy-pass",
        webshare_domain="p.webshare.io",
        webshare_port=80,
    )


def _context(
    ledger: RecordingLedger,
    *,
    estimated_units: dict[str, float] | None = None,
    balance_units: dict[str, float] | None = None,
) -> ProviderCallContext:
    return ProviderCallContext(
        gate=UsageGate(),
        ledger=ledger,
        workspace_id="ws_webshare",
        subject="webshare",
        operation="proxy_fetch",
        estimated_units=estimated_units or {"request_count": 1},
        allocation=ProviderAllocation(
            id="alloc_webshare",
            workspace_id="ws_webshare",
            provider="webshare",
            operation="proxy_fetch",
        ),
        policy=EntitlementPolicy(
            id="policy_webshare",
            workspace_id="ws_webshare",
            allowed_operations={"webshare.proxy_fetch"},
        ),
        balance=WorkspaceBalance(
            workspace_id="ws_webshare",
            remaining_units={"request_count": 10} if balance_units is None else balance_units,
        ),
        idempotency_key="ws_webshare:video123:webshare.proxy_fetch:test",
        metadata={"job_id": "job_webshare"},
    )


def test_proxy_url_default_behavior_stays_unmetered() -> None:
    url = proxy_url_for_ytdlp(_webshare_proxy(), key="video123")

    assert url == "http://proxy-user-rotate:proxy-pass@p.webshare.io:80/"


def test_hosted_proxy_url_denial_blocks_url() -> None:
    ledger = RecordingLedger()
    context = _context(ledger, estimated_units={"request_count": 2}, balance_units={"request_count": 1})

    with pytest.raises(UsageReservationDenied):
        proxy_url_for_ytdlp(_webshare_proxy(), key="video123", hosted_context=context)

    assert [event.status for event in ledger.events] == ["denied"]
    assert ledger.events[0].error_code == "insufficient_balance"


def test_hosted_fetch_video_metadata_records_success_and_uses_proxy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = RecordingLedger()
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = json.dumps({"id": "video123", "title": "Hosted test", "upload_date": "20260527"}) + "\n"
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr("yutome.youtube.subprocess.run", fake_run)

    metadata = fetch_video_metadata(
        video_id="video123",
        cwd=tmp_path,
        proxy=_webshare_proxy(),
        hosted_context=_context(ledger),
    )

    assert metadata["id"] == "video123"
    assert commands
    proxy_flag_index = commands[0].index("--proxy")
    assert commands[0][proxy_flag_index + 1] == "http://proxy-user-rotate:proxy-pass@p.webshare.io:80/"
    assert "--no-js-runtimes" in commands[0]
    assert "--no-remote-components" in commands[0]
    assert [event.status for event in ledger.events] == ["started", "succeeded"]
    succeeded = ledger.events[1]
    assert succeeded.subject == "webshare"
    assert succeeded.operation == "proxy_fetch"
    assert succeeded.actual_units["request_count"] == 1
    assert succeeded.actual_units["local_response_bytes"] == len(
        (json.dumps({"id": "video123", "title": "Hosted test", "upload_date": "20260527"}) + "\n").encode("utf-8")
    )
    assert succeeded.actual_units["local_request_bytes"] > 0
    assert succeeded.actual_units["bytes"] >= succeeded.actual_units["local_response_bytes"]
    assert succeeded.metadata["accounting_source"] == "yt-dlp"
    assert succeeded.metadata["byte_accounting_status"] == "locally_visible_transfer"
    assert succeeded.metadata["byte_accounting_basis"] == "yt_dlp_stdout_plus_target_request_estimate"
    assert succeeded.metadata["provider_byte_accounting"] == "async_webshare_proxy_activity_or_stats"
    assert succeeded.metadata["provider_bytes_exact_for_call"] is False
    assert succeeded.metadata["job_id"] == "job_webshare"


def test_hosted_fetch_video_metadata_records_proxy_quota_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = RecordingLedger()

    def fake_run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="CONNECT tunnel failed, response 402",
        )

    monkeypatch.setattr("yutome.youtube.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="Webshare proxy returned 402 Payment Required"):
        fetch_video_metadata(
            video_id="video123",
            cwd=tmp_path,
            proxy=_webshare_proxy(),
            hosted_context=_context(ledger),
        )

    assert [event.status for event in ledger.events] == ["started", "failed"]
    failed = ledger.events[1]
    assert failed.error_code == "http_402"
    assert failed.metadata["failure_kind"] == "quota"
    assert failed.metadata["retryable"] is False


def test_hosted_transcript_denial_prevents_proxy_api_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = RecordingLedger()
    context = _context(ledger, estimated_units={"request_count": 2}, balance_units={"request_count": 1})

    class FailIfConstructed:
        def __init__(self, **_: Any) -> None:
            raise AssertionError("transcript API must not be constructed after hosted denial")

    monkeypatch.setattr("yutome.youtube.YouTubeTranscriptApi", FailIfConstructed)

    with pytest.raises(UsageReservationDenied):
        fetch_transcript(
            video_id="video123",
            languages=["en"],
            proxy=_webshare_proxy(),
            hosted_context=context,
        )

    assert [event.status for event in ledger.events] == ["denied"]
