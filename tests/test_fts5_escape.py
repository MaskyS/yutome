"""FTS5 query-syntax escape tests.

User input passed verbatim to ``WHERE chunks_fts MATCH ?`` crashes on
characters that FTS5 treats as operators (`-`, `:`, `*`, `+`, `(`, `)`,
`^`). v0.1.2 wraps non-raw queries as FTS5 phrases per §3.1 of
https://www.sqlite.org/fts5.html so those characters are literal.

This module covers:

* hyphen / colon / asterisk / parens / caret no longer crash
* embedded `"` in the query string is doubled inside the phrase
* `--raw` (via ``Search.raw=True``) bypasses the wrapper so power users
  can write FTS5 operators verbatim
* video-title lexical search applies the same escape inside the
  column-filter wrapper
"""
from __future__ import annotations

from pathlib import Path

import pytest

from yutome.api import find
from yutome.config import default_config
from yutome.db import bootstrap_catalog, connect_catalog
from yutome.paths import ProjectPaths
from yutome.query import _fts5_phrase, Search
from yutome.store import rebuild_fts


@pytest.fixture
def fixture_corpus(tmp_path: Path) -> tuple[object, ProjectPaths]:
    """Build a tiny indexed corpus with content that exercises the FTS path."""
    config = default_config()
    paths = ProjectPaths.from_config(config, project_root=tmp_path)
    paths.ensure_base_dirs()
    bootstrap_catalog(paths.catalog_db)
    with connect_catalog(paths.catalog_db) as connection:
        connection.execute(
            "INSERT INTO channels(channel_id, handle, source_url, title) "
            "VALUES ('chan1', '@example', 'https://www.youtube.com/@example', 'Example')"
        )
        connection.execute(
            "INSERT INTO videos(video_id, channel_id, title, duration_seconds, ingest_status) "
            "VALUES ('vid1', 'chan1', 'state-of-the-art results', 60, 'indexed')"
        )
        connection.execute(
            "INSERT INTO transcript_versions(transcript_version_id, video_id, source, language, "
            "is_generated, raw_path, normalized_path, text_hash, segment_count, active) "
            "VALUES ('tx1', 'vid1', 'youtube-transcript-api', 'en', 1, '', '', 'h', 1, 1)"
        )
        connection.execute(
            "INSERT INTO chunks(chunk_id, transcript_version_id, video_id, channel_id, sequence, "
            "start_ms, end_ms, text, token_count, text_hash, chunker_version) "
            "VALUES ('c1', 'tx1', 'vid1', 'chan1', 0, 0, 4000, "
            "'A discussion of state-of-the-art protocols and prefix*search.', "
            "10, 'ch', 'v')"
        )
        rebuild_fts(connection)
        connection.commit()
    return config, paths


def test_phrase_helper_wraps_and_escapes_embedded_quotes() -> None:
    assert _fts5_phrase("plain") == '"plain"'
    assert _fts5_phrase("term-with-hyphens") == '"term-with-hyphens"'
    assert _fts5_phrase('embed "quoted" word') == '"embed ""quoted"" word"'
    assert _fts5_phrase("") == '""'


def test_find_with_hyphens_no_longer_crashes(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    """v0.1.1: `yutome search find term-with-hyphens` raised
    `OperationalError: no such column: nonexistent` because FTS5
    interpreted the hyphen as a negative column-filter prefix."""
    config, paths = fixture_corpus
    result = find(config=config, paths=paths, text="term-with-hyphens", mode="lexical")
    assert result.rows == []  # no match, but importantly no exception
    assert any("No matches" in note for note in result.notes)


def test_find_with_colon_no_longer_crashes(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    config, paths = fixture_corpus
    result = find(config=config, paths=paths, text="title:foo", mode="lexical")
    assert result.rows == []


def test_find_with_asterisk_no_longer_crashes(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    config, paths = fixture_corpus
    result = find(config=config, paths=paths, text="prefix*search", mode="lexical")
    # The exact phrase appears in the seeded chunk.
    assert result.rows
    assert result.rows[0]["video_id"] == "vid1"


def test_find_phrase_matches_hyphenated_text(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    """Default lexical mode now does phrase match: the literal hyphenated
    phrase should match a chunk that contains exactly that sequence."""
    config, paths = fixture_corpus
    result = find(config=config, paths=paths, text="state-of-the-art", mode="lexical")
    assert result.rows
    assert result.rows[0]["video_id"] == "vid1"


def test_find_with_embedded_quote_no_crash(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    config, paths = fixture_corpus
    result = find(config=config, paths=paths, text='a "phrase" b', mode="lexical")
    assert isinstance(result.rows, list)  # either matches or doesn't, but no crash


def test_find_titles_lexical_handles_hyphens(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    """The video-title FTS path wraps inside `column:(...)`. The fix has
    to escape there too or hyphens still crash."""
    config, paths = fixture_corpus
    result = find(
        config=config, paths=paths, text="state-of-the-art", in_="titles", mode="lexical"
    )
    assert result.rows
    assert result.rows[0]["video_id"] == "vid1"


def test_raw_mode_passes_fts5_operators_through(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    """`raw=True` opts out of phrase wrapping so power users can use
    FTS5 boolean operators."""
    config, paths = fixture_corpus
    result = find(
        config=config,
        paths=paths,
        text="state OR prefix",
        mode="lexical",
        raw=True,
    )
    assert result.rows
    assert result.rows[0]["video_id"] == "vid1"


def test_non_raw_treats_OR_as_phrase_not_operator(fixture_corpus: tuple[object, ProjectPaths]) -> None:
    """Without --raw, `OR` is part of the phrase, not the boolean operator.
    The seeded chunk does not contain the literal phrase "state OR prefix"."""
    config, paths = fixture_corpus
    result = find(config=config, paths=paths, text="state OR prefix", mode="lexical")
    assert result.rows == []


def test_search_model_default_raw_is_false() -> None:
    assert Search().raw is False
