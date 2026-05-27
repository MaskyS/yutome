from __future__ import annotations

import random
import sqlite3
import time
from importlib.util import find_spec
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, TypeAlias

from yutome.config import AppConfig

if TYPE_CHECKING:
    from yutome.hosted.provider_wrappers import ProviderCallContext

LANCEDB_CHUNKS_TABLE = "chunks"
LANCEDB_REQUIRED_COLUMNS = {
    "chunk_id",
    "channel_id",
    "video_id",
    "transcript_version_id",
    "source",
    "language",
    "is_generated",
    "sequence",
    "start_ms",
    "end_ms",
    "text",
    "token_count",
    "text_hash",
    "chunker_version",
    "active",
    "embedding_model",
    "embedding_dim",
    "vector",
}

VoyageHostedContextFactory: TypeAlias = Callable[
    [list[dict[str, Any]]],
    "ProviderCallContext | None",
]


@dataclass(frozen=True)
class EmbeddingStats:
    embedded_chunks: int
    skipped: bool = False
    message: str = ""
    failed_batches: int = 0


def _batched(rows: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), batch_size):
        yield rows[index : index + batch_size]


def _retryable_embedding_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if status_code in {408, 429, 500, 502, 503, 504}:
        return True
    text = str(error).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "too many requests",
            "timeout",
            "temporarily",
            "connection",
            "server error",
            "503",
            "504",
        )
    )


def _embed_voyage_batch(
    batch: list[dict[str, Any]],
    *,
    model: str,
    dimension: int,
    max_retries: int,
    retry_base_seconds: float,
    hosted_context: ProviderCallContext | None = None,
) -> list[dict[str, Any]]:
    import voyageai

    client: Any | None = None
    texts = [row["text"] for row in batch]

    def call() -> Any:
        nonlocal client
        if client is None:
            client = voyageai.Client()
        return client.embed(
            texts,
            model=model,
            input_type="document",
            output_dimension=dimension,
        )

    response = _execute_hosted_voyage_call(
        lambda: _call_with_retries(
            call,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        ),
        hosted_context=hosted_context,
        input_type="document",
        output_dimension=dimension,
    )
    return [
        {
            "chunk_id": row["chunk_id"],
            "channel_id": row["channel_id"] or "",
            "video_id": row["video_id"],
            "transcript_version_id": row["transcript_version_id"],
            "source": row["source"],
            "language": row["language"] or "",
            "is_generated": bool(row["is_generated"]),
            "sequence": row["sequence"],
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "text": row["text"],
            "token_count": row["token_count"] or 0,
            "text_hash": row["text_hash"],
            "chunker_version": row["chunker_version"],
            "active": True,
            "embedding_model": model,
            "embedding_dim": dimension,
            "vector": vector,
        }
        for row, vector in zip(batch, response.embeddings, strict=True)
    ]


def _embed_voyage_query(
    *,
    query: str,
    model: str,
    dimension: int,
    hosted_context: ProviderCallContext | None = None,
) -> list[float]:
    import voyageai

    client: Any | None = None

    def call() -> Any:
        nonlocal client
        if client is None:
            client = voyageai.Client()
        return client.embed(
            [query],
            model=model,
            input_type="query",
            output_dimension=dimension,
        )

    response = _execute_hosted_voyage_call(
        call,
        hosted_context=hosted_context,
        input_type="query",
        output_dimension=dimension,
    )
    return response.embeddings[0]


def _execute_hosted_voyage_call(
    call: Callable[[], Any],
    *,
    hosted_context: ProviderCallContext | None,
    input_type: str,
    output_dimension: int,
) -> Any:
    if hosted_context is None:
        return call()

    from yutome.hosted.normalizers import normalize_voyage_embeddings_response
    from yutome.hosted.provider_wrappers import execute_provider_call

    return execute_provider_call(
        hosted_context,
        call,
        normalize_usage=lambda response: normalize_voyage_embeddings_response(
            response,
            operation=hosted_context.operation,
            input_type=input_type,
            output_dimension=output_dimension,
        ),
    )


def _call_with_retries(
    call: Callable[[], Any],
    *,
    max_retries: int,
    retry_base_seconds: float,
) -> Any:
    for attempt in range(max_retries + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - provider clients expose mixed exception types.
            if attempt >= max_retries or not _retryable_embedding_error(exc):
                raise
            sleep_seconds = retry_base_seconds * (2**attempt)
            if retry_base_seconds:
                sleep_seconds += random.uniform(0, retry_base_seconds)
            time.sleep(sleep_seconds)
    raise AssertionError("unreachable retry loop exit")


def rebuild_lancedb_chunks(
    *,
    connection: sqlite3.Connection,
    config: AppConfig,
    lancedb_dir: Path,
) -> EmbeddingStats:
    if config.vectors.backend != "lancedb" or not config.vectors.enabled:
        return EmbeddingStats(embedded_chunks=0, skipped=True, message="LanceDB vector backend disabled")
    try:
        import lancedb
    except ImportError as exc:
        return EmbeddingStats(embedded_chunks=0, skipped=True, message=f"missing optional dependency: {exc.name}")

    db = lancedb.connect(lancedb_dir)
    if _lancedb_has_table(db, LANCEDB_CHUNKS_TABLE):
        db.drop_table(LANCEDB_CHUNKS_TABLE)
    connection.execute(
        """
        DELETE FROM embeddings
        WHERE provider = ? AND model = ? AND dimension = ?
        """,
        (config.embeddings.provider, config.embeddings.model, config.embeddings.dimension),
    )
    connection.commit()
    return embed_pending_chunks(connection=connection, config=config, lancedb_dir=lancedb_dir)


def _lancedb_table_names(db) -> list[str]:
    tables = db.list_tables()
    if isinstance(tables, list):
        return tables
    value = getattr(tables, "tables", None)
    if isinstance(value, list):
        return value
    return []


def _lancedb_has_table(db, table_name: str) -> bool:
    return table_name in _lancedb_table_names(db)


def _lancedb_missing_chunk_columns(table) -> set[str]:
    names = set(getattr(table.schema, "names", []))
    return LANCEDB_REQUIRED_COLUMNS - names


def ensure_lancedb_chunk_indexes(table) -> None:
    try:
        table.create_fts_index("text", replace=True)
    except Exception as exc:  # noqa: BLE001 - surface index issues during hybrid queries.
        raise RuntimeError(f"failed to create LanceDB text index; run `yutome corpus rebuild vectors`: {exc}") from exc
    try:
        table.optimize()
    except Exception:
        pass


def embed_pending_chunks(
    *,
    connection: sqlite3.Connection,
    config: AppConfig,
    lancedb_dir: Path,
    limit: int | None = None,
    batch_size: int | None = None,
    concurrency: int | None = None,
    hosted_context_factory: VoyageHostedContextFactory | None = None,
) -> EmbeddingStats:
    if not config.embeddings.enabled:
        return EmbeddingStats(embedded_chunks=0, skipped=True, message="embeddings disabled")
    if config.vectors.backend != "lancedb" or not config.vectors.enabled:
        return EmbeddingStats(embedded_chunks=0, skipped=True, message="LanceDB vector backend disabled")

    try:
        import lancedb
    except ImportError as exc:
        return EmbeddingStats(embedded_chunks=0, skipped=True, message=f"missing optional dependency: {exc.name}")
    if find_spec("voyageai") is None:
        return EmbeddingStats(embedded_chunks=0, skipped=True, message="missing optional dependency: voyageai")

    provider = config.embeddings.provider
    model = config.embeddings.model
    dimension = config.embeddings.dimension
    if provider != "voyage":
        return EmbeddingStats(embedded_chunks=0, skipped=True, message=f"unsupported embedding provider: {provider}")

    sql = """
        SELECT
            c.chunk_id,
            c.channel_id,
            c.video_id,
            c.transcript_version_id,
            tv.source,
            tv.language,
            tv.is_generated,
            c.sequence,
            c.start_ms,
            c.end_ms,
            c.text,
            c.token_count,
            c.text_hash,
            c.chunker_version
        FROM chunks c
        JOIN transcript_versions tv
            ON tv.transcript_version_id = c.transcript_version_id
            AND tv.active = 1
        LEFT JOIN embeddings e
            ON e.chunk_id = c.chunk_id
            AND e.provider = ?
            AND e.model = ?
            AND e.dimension = ?
            AND e.index_status = 'indexed'
        WHERE e.chunk_id IS NULL
        ORDER BY c.video_id, c.sequence
    """
    if limit is not None:
        sql += " LIMIT ?"
        params: tuple[object, ...] = (provider, model, dimension, limit)
    else:
        params = (provider, model, dimension)
    rows = [dict(row) for row in connection.execute(sql, params).fetchall()]
    if not rows:
        return EmbeddingStats(embedded_chunks=0, message="no pending chunks")

    db = lancedb.connect(lancedb_dir)
    table = None
    embedded = 0
    failed_batches = 0
    last_error = ""
    if _lancedb_has_table(db, LANCEDB_CHUNKS_TABLE):
        table = db.open_table(LANCEDB_CHUNKS_TABLE)
        missing_columns = _lancedb_missing_chunk_columns(table)
        if missing_columns:
            db.drop_table(LANCEDB_CHUNKS_TABLE)
            table = None

    effective_batch_size = batch_size or config.embeddings.batch_size
    effective_concurrency = max(1, concurrency or config.embeddings.concurrency)
    batches = list(_batched(rows, effective_batch_size))
    max_workers = min(effective_concurrency, len(batches))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _embed_voyage_batch,
                batch,
                model=model,
                dimension=dimension,
                max_retries=config.embeddings.max_retries,
                retry_base_seconds=config.embeddings.retry_base_seconds,
                hosted_context=(
                    hosted_context_factory(batch)
                    if hosted_context_factory is not None
                    else None
                ),
            )
            for batch in batches
        ]
        for future in as_completed(futures):
            try:
                records = future.result()
            except Exception as exc:  # noqa: BLE001 - leave failed batches pending for resume.
                failed_batches += 1
                last_error = str(exc)
                continue
            if not records:
                continue
            if table is None:
                table = db.create_table(LANCEDB_CHUNKS_TABLE, data=records, mode="overwrite")
            else:
                table.add(records)
            connection.executemany(
                """
                INSERT INTO embeddings(chunk_id, provider, model, dimension, artifact_status, index_status, embedded_at)
                VALUES (?, ?, ?, ?, 'stored', 'indexed', datetime('now'))
                ON CONFLICT(chunk_id, provider, model, dimension) DO UPDATE SET
                    artifact_status = 'stored',
                    index_status = 'indexed',
                    embedded_at = datetime('now')
                """,
                [(record["chunk_id"], provider, model, dimension) for record in records],
            )
            connection.commit()
            embedded += len(records)

    if table is not None and embedded:
        ensure_lancedb_chunk_indexes(table)

    message = ""
    if failed_batches:
        message = f"{failed_batches} embedding batch(es) failed and remain pending"
        if last_error:
            message += f": {last_error[:200]}"
    return EmbeddingStats(embedded_chunks=embedded, message=message, failed_batches=failed_batches)
