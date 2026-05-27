from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from yutome.cli import app
from yutome.hosted.cli_helpers import (
    DemoUsageEventSpec,
    append_demo_usage_event,
    append_demo_usage_events,
    summarize_usage_events,
    summarize_usage_ledger,
)
from yutome.hosted.ledger import JsonlUsageLedger
from yutome.hosted.models import UsageEvent


def test_append_demo_usage_event_writes_real_jsonl_visible_to_usage_command(tmp_path: Path) -> None:
    ledger_path = tmp_path / "usage_events.jsonl"

    event = append_demo_usage_event(
        ledger_path,
        workspace_id="ws_smoke",
        subject="voyage",
        operation="embed_documents",
        actual_units={"total_tokens": 42, "vectors": 2},
        provider_request_id="demo_request",
        metadata={"diagnostic": "usage-command"},
    )

    assert event.metadata["synthetic"] is True
    assert event.metadata["diagnostic"] == "usage-command"
    assert event.raw_usage["synthetic"] is True
    assert JsonlUsageLedger(ledger_path).recent(limit=1)[0].id == event.id

    result = CliRunner().invoke(app, ["hosted", "usage", "--ledger", str(ledger_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload[0]["workspace_id"] == "ws_smoke"
    assert payload[0]["subject"] == "voyage"
    assert payload[0]["operation"] == "embed_documents"
    assert payload[0]["provider_request_id"] == "demo_request"
    assert payload[0]["actual_units"]["total_tokens"] == 42


def test_usage_command_can_append_demo_events_and_print_summary(tmp_path: Path) -> None:
    ledger_path = tmp_path / "usage_events.jsonl"

    result = CliRunner().invoke(
        app,
        ["hosted", "usage", "--ledger", str(ledger_path), "--append-demo", "--summary", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [row["operation_key"] for row in payload] == [
        "search_store.hybrid_query",
        "voyage.embed_documents",
    ]
    assert payload[0]["event_count"] == 1
    assert payload[0]["status_counts"] == {"succeeded": 1}
    assert payload[0]["unit_totals"]["queries"] == 1.0
    assert JsonlUsageLedger(ledger_path).recent(limit=10)


def test_append_demo_usage_events_uses_default_smoke_rows(tmp_path: Path) -> None:
    ledger_path = tmp_path / "usage_events.jsonl"

    events = append_demo_usage_events(ledger_path, workspace_id="ws_demo", metadata={"run_id": "smoke_1"})

    assert [event.operation_key for event in events] == [
        "search_store.hybrid_query",
        "voyage.embed_documents",
    ]
    assert all(event.workspace_id == "ws_demo" for event in events)
    assert all(event.metadata["run_id"] == "smoke_1" for event in events)
    assert [event.operation_key for event in JsonlUsageLedger(ledger_path).recent(limit=10)] == [
        "search_store.hybrid_query",
        "voyage.embed_documents",
    ]


def test_append_demo_usage_events_accepts_empty_specs(tmp_path: Path) -> None:
    ledger_path = tmp_path / "usage_events.jsonl"

    events = append_demo_usage_events(ledger_path, [])

    assert events == []
    assert not ledger_path.exists()


def test_summarize_usage_ledger_totals_by_subject_operation(tmp_path: Path) -> None:
    ledger_path = tmp_path / "usage_events.jsonl"
    append_demo_usage_events(
        ledger_path,
        [
            DemoUsageEventSpec(
                subject="voyage",
                operation="embed_documents",
                actual_units={"total_tokens": 100, "vectors": 2, "cached": True},
            ),
            DemoUsageEventSpec(
                subject="voyage",
                operation="embed_documents",
                actual_units={"total_tokens": 50.5, "vectors": 1, "provider_tier": "trial"},
            ),
            DemoUsageEventSpec(
                subject="gemini",
                operation="cleanup_transcript",
                actual_units={"total_tokens": 32},
                status="failed",
            ),
        ],
    )

    summaries = summarize_usage_ledger(ledger_path)

    assert [summary.operation_key for summary in summaries] == [
        "gemini.cleanup_transcript",
        "voyage.embed_documents",
    ]
    assert summaries[0].event_count == 1
    assert summaries[0].status_counts == {"failed": 1}
    assert summaries[0].unit_totals == {"total_tokens": 32}
    assert summaries[1].event_count == 2
    assert summaries[1].status_counts == {"succeeded": 2}
    assert summaries[1].unit_totals == {"total_tokens": 150.5, "vectors": 3}


def test_summarize_usage_events_supports_in_memory_repl_diagnostics() -> None:
    events = [
        UsageEvent(
            workspace_id="ws_alice",
            subject="search_store",
            operation="hybrid_query",
            event_type="service_operation_succeeded",
            status="succeeded",
            actual_units={"queries": 1},
        ),
        UsageEvent(
            workspace_id="ws_alice",
            subject="search_store",
            operation="hybrid_query",
            event_type="service_operation_succeeded",
            status="succeeded",
            actual_units={"queries": 1, "result_count": 12},
        ),
    ]

    summaries = summarize_usage_events(events)

    assert len(summaries) == 1
    assert summaries[0].operation_key == "search_store.hybrid_query"
    assert summaries[0].unit_totals == {"queries": 2, "result_count": 12}
