"""Tests for the yt-dlp metadata row validation.

The validation exists because the catalog accumulated 212 stub rows with
`_type='url'` and `upload_date=None` over historical sync runs. The check
makes those failures loud instead of silent.
"""
from __future__ import annotations

import pytest

from ytkb.store import _published_at_from_flat_discovery
from ytkb.youtube import _validate_video_metadata_row


def test_rejects_url_stub() -> None:
    stub = {"_type": "url", "id": "abc", "title": "Some Title", "upload_date": None}
    with pytest.raises(RuntimeError, match="stub"):
        _validate_video_metadata_row(stub, video_id="abc")


def test_rejects_url_transparent_stub() -> None:
    stub = {"_type": "url_transparent", "id": "abc", "title": "Some Title"}
    with pytest.raises(RuntimeError, match="stub"):
        _validate_video_metadata_row(stub, video_id="abc")


def test_accepts_real_video_extraction() -> None:
    row = {
        "_type": "video",
        "id": "abc",
        "title": "Real Video",
        "upload_date": "20240716",
    }
    _validate_video_metadata_row(row, video_id="abc")


def test_accepts_missing_type_field() -> None:
    # Some yt-dlp versions omit _type on a full video extraction.
    row = {"id": "abc", "title": "Real Video", "upload_date": "20240716"}
    _validate_video_metadata_row(row, video_id="abc")


def test_rejects_row_with_no_identifier_or_title() -> None:
    row = {"_type": "video", "id": None, "title": None}
    with pytest.raises(RuntimeError, match="no id or title"):
        _validate_video_metadata_row(row, video_id="abc")


def test_error_message_includes_video_id() -> None:
    stub = {"_type": "url", "title": "X"}
    with pytest.raises(RuntimeError, match="ZZZ"):
        _validate_video_metadata_row(stub, video_id="ZZZ")


def test_flat_discovery_published_at_prefers_upload_date() -> None:
    raw = {"upload_date": "20260507", "timestamp": 1778112000}

    assert _published_at_from_flat_discovery(raw) == "20260507"


def test_flat_discovery_published_at_falls_back_to_timestamp() -> None:
    raw = {"timestamp": 1778112000}

    assert _published_at_from_flat_discovery(raw) == "20260507"


def test_flat_discovery_published_at_ignores_bad_timestamp() -> None:
    raw = {"timestamp": "not-a-timestamp"}

    assert _published_at_from_flat_discovery(raw) is None
