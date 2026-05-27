from __future__ import annotations

from dataclasses import asdict

from yutome.hosted.cli_helpers import summarize_usage_events
from yutome.hosted.models import UsageEvent


def test_summarize_usage_events_totals_by_subject_operation() -> None:
    events = [
        UsageEvent(
            workspace_id="ws_alice",
            subject="voyage",
            operation="embed_documents",
            event_type="provider_attempt_succeeded",
            status="succeeded",
            actual_units={"total_tokens": 100, "vectors": 2, "cached": True},
        ),
        UsageEvent(
            workspace_id="ws_alice",
            subject="voyage",
            operation="embed_documents",
            event_type="provider_attempt_succeeded",
            status="succeeded",
            actual_units={"total_tokens": 50.5, "vectors": 1, "provider_tier": "trial"},
        ),
        UsageEvent(
            workspace_id="ws_alice",
            subject="gemini",
            operation="cleanup_transcript",
            event_type="provider_attempt_failed",
            status="failed",
            actual_units={"total_tokens": 32},
        ),
    ]

    summaries = summarize_usage_events(events)

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


def test_summarize_usage_events_is_json_serializable() -> None:
    events = [
        UsageEvent(
            workspace_id="ws_alice",
            subject="search_store",
            operation="hybrid_query",
            event_type="service_operation_succeeded",
            status="succeeded",
            actual_units={"queries": 1},
        )
    ]

    payload = [asdict(summary) for summary in summarize_usage_events(events)]

    assert payload == [
        {
            "subject": "search_store",
            "operation": "hybrid_query",
            "event_count": 1,
            "unit_totals": {"queries": 1},
            "status_counts": {"succeeded": 1},
        }
    ]
