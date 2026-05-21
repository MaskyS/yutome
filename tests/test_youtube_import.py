from __future__ import annotations

import pytest

from yutome.youtube_import import (
    YouTubeImportError,
    _channels_from_initial_data,
    _extract_yt_initial_data,
    fetch_public_subscription_channels_from_api,
    fetch_public_subscription_channels_from_scrape,
)


def test_initial_data_parser_extracts_channel_renderers() -> None:
    html = """
    <script>
    var ytInitialData = {"contents":{"items":[{"channelRenderer":{
      "channelId":"UCabc12345678901234567890",
      "title":{"simpleText":"Example Channel"},
      "navigationEndpoint":{"browseEndpoint":{"canonicalBaseUrl":"/@Example"}}
    }}]}};
    </script>
    """

    data = _extract_yt_initial_data(html)
    channels = _channels_from_initial_data(data, import_source="youtube-public-scrape")

    assert len(channels) == 1
    assert channels[0].channel_id == "UCabc12345678901234567890"
    assert channels[0].title == "Example Channel"
    assert channels[0].import_source == "youtube-public-scrape"


def test_public_api_import_paginates_and_marks_source(monkeypatch) -> None:  # noqa: ANN001
    payloads = [
        {
            "items": [
                {
                    "snippet": {
                        "title": "One",
                        "resourceId": {"channelId": "UC1111111111111111111111"},
                    }
                }
            ],
            "nextPageToken": "next",
        },
        {
            "items": [
                {
                    "snippet": {
                        "title": "Two",
                        "resourceId": {"channelId": "UC2222222222222222222222"},
                    }
                }
            ]
        },
    ]

    def fake_read_json(url: str):  # noqa: ANN001
        assert "key=test-key" in url
        return payloads.pop(0)

    monkeypatch.setattr("yutome.youtube_import._read_json", fake_read_json)

    channels = fetch_public_subscription_channels_from_api(
        "UCsource123456789012345678",
        api_key="test-key",
    )

    assert [channel.title for channel in channels] == ["One", "Two"]
    assert {channel.import_source for channel in channels} == {"youtube-public-api"}


def test_public_scrape_fails_cleanly_without_channel_renderers(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "yutome.youtube_import._read_url",
        lambda url: 'var ytInitialData = {"contents":{"items":[]}};',
    )

    with pytest.raises(YouTubeImportError, match="No public subscription channels"):
        fetch_public_subscription_channels_from_scrape("@PrivateSource")
