from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import case, func, literal_column
from sqlalchemy.dialects.postgresql import insert

from yutome.chunking import CHUNKER_VERSION, build_chunks
from yutome.config import AppConfig, ProxyConfig
from yutome.gemini import transcribe_youtube_url_with_gemini
from yutome.hashing import sha256_json
from yutome.hosted.allocations import Allocation, resolve_allocation
from yutome.hosted.allocation_policy import default_search_store_allocation
from yutome.hosted.control_plane import (
    Job,
    JobOperation,
    Source,
    job_operation_idempotency_key,
    source_discovery_decision,
)
from yutome.hosted.entitlements import PostgresUsageContextProvider
from yutome.hosted.errors import redact_sensitive_failure_text
from yutome.hosted.events import denied_usage_event, usage_event_from_normalization
from yutome.hosted.gate import UsageGate
from yutome.hosted.ids import input_hash
from yutome.hosted.ledger import PostgresUsageGate, PostgresUsageLedger, stable_usage_reservation_id
from yutome.hosted.migrations import (
    HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    HOSTED_DEFAULT_EMBEDDING_MODEL,
    HOSTED_DEFAULT_TOKENIZER,
    HOSTED_VECTOR_BACKEND,
)
from yutome.hosted.mcp_query import HostedMcpAuthContext, HostedMcpUsageContext
from yutome.hosted.models import (
    EntitlementPolicy,
    ProviderAllocation,
    UsageEvent,
    UsageNormalization,
    UsageReservation,
    UsageSubject,
    WorkspaceBalance,
)
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied, execute_provider_call
from yutome.hosted.repositories import SqlStatement, upsert_usage_reservation_sql
from yutome.hosted.schema import job_operations, jobs, search_index_profiles, transcript_versions, videos
from yutome.hosted.search_store import (
    SearchStoreQueryPlan,
    hybrid_query_plan,
    replace_active_transcript_sql,
    validate_supported_embedding_profile,
)
from yutome.hosted.sqlalchemy_core import compile_postgres_statement
from yutome.quality_llm import TranscriptCleanupContext, cleanup_transcript_with_gemini
from yutome.transcripts import NormalizedTranscript, TranscriptSegment, normalize_transcript
from yutome.youtube import (
    DiscoveredVideo,
    TranscriptFetchResult,
    discover_video,
    discover_videos,
    fetch_subtitle_transcript_with_ytdlp,
    fetch_transcript,
    is_proxy_payment_error,
    is_youtube_block_error,
)


DEFAULT_INDEX_PROFILE_ID = "sip_voyage4lite_bm25_default"
DEFAULT_EMBEDDING_MODEL = HOSTED_DEFAULT_EMBEDDING_MODEL
DEFAULT_EMBEDDING_DIMENSION = HOSTED_DEFAULT_EMBEDDING_DIMENSION
DEFAULT_CHUNKING_VERSION = "hosted_mock_chunker_v1"
REAL_HOSTED_CHUNKING_VERSION = CHUNKER_VERSION
YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class HostedVideoInput:
    youtube_video_id: str
    title: str
    url: str
    channel_id: str | None = None
    description: str = ""
    duration_seconds: int | None = None
    published_at: datetime | None = None
    metadata: Mapping[str, Any] | None = None


def _hosted_ytdlp_published_at(metadata: Mapping[str, Any]) -> datetime | None:
    for key in ("upload_date", "release_date", "modified_date"):
        parsed = _hosted_ytdlp_date(metadata.get(key))
        if parsed is not None:
            return parsed
    timestamp = _float_or_none(metadata.get("timestamp"))
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _hosted_ytdlp_date(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d{8}", text):
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _hosted_ytdlp_thumbnail_url(metadata: Mapping[str, Any]) -> str | None:
    if thumbnail := _text_or_none(metadata.get("thumbnail")):
        return thumbnail
    thumbnails = metadata.get("thumbnails")
    if isinstance(thumbnails, Sequence) and not isinstance(thumbnails, (str, bytes)):
        for thumbnail in reversed(thumbnails):
            if isinstance(thumbnail, Mapping) and (url := _text_or_none(thumbnail.get("url"))):
                return url
    return None


def _hosted_ytdlp_metadata(discovered: DiscoveredVideo) -> dict[str, Any]:
    raw = discovered.raw or {}
    metadata = {
        "source": "yt_dlp",
        "channel_title": discovered.channel_title,
        "channel_handle": discovered.channel_handle,
        "playlist_tab": discovered.playlist_tab,
        "thumbnail_url": _hosted_ytdlp_thumbnail_url(raw),
        "webpage_url": _text_or_none(raw.get("webpage_url")) or discovered.url,
        "live_status": _text_or_none(raw.get("live_status")),
        "upload_date": raw.get("upload_date"),
        "release_date": raw.get("release_date"),
        "timestamp": raw.get("timestamp"),
        "metadata_hash": sha256_json(raw),
    }
    return {key: value for key, value in metadata.items() if value is not None}


@dataclass(frozen=True)
class TranscriptChunkInput:
    chunk_index: int
    text: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class IndexProfileInput:
    id: str = DEFAULT_INDEX_PROFILE_ID
    backend: str = HOSTED_VECTOR_BACKEND
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    chunking_version: str = DEFAULT_CHUNKING_VERSION
    tokenizer: str = HOSTED_DEFAULT_TOKENIZER
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class PlannedSqlOperation:
    name: str
    statement: SqlStatement
    operation_id: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class HostedIndexingPlan:
    source: Source
    job: Job
    video: HostedVideoInput
    hosted_video_id: str
    transcript_version_id: str
    transcript_content_hash: str
    index_profile: IndexProfileInput
    job_operations: tuple[JobOperation, ...]
    usage_reservations: tuple[UsageReservation, ...]
    sql_operations: tuple[PlannedSqlOperation, ...]
    search_operations: tuple[SearchStoreQueryPlan, ...]

    @property
    def operation_ids(self) -> tuple[str, ...]:
        return tuple(operation.id for operation in self.job_operations)


@dataclass(frozen=True)
class HostedIndexingExecutionResult:
    job_id: str
    workspace_id: str
    source_id: str
    youtube_video_id: str | None
    status: Literal["succeeded", "denied", "failed", "retry_wait", "cancelled"]
    hosted_video_id: str | None = None
    transcript_version_id: str | None = None
    chunks_written: int = 0
    embeddings_written: int = 0
    denied_operation: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class HostedSourceDiscoveryExecutionResult:
    job_id: str
    workspace_id: str
    source_id: str
    status: Literal["succeeded", "failed", "retry_wait", "denied"]
    discovered_videos: int = 0
    enqueued_jobs: int = 0
    video_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None


class HostedIndexingError(RuntimeError):
    code = "hosted_indexing_failed"


class HostedWebshareRequired(HostedIndexingError):
    code = "webshare_required"


class HostedIndexingLostLease(HostedIndexingError):
    code = "job_lease_lost"


class HostedProviderOutputMissing(HostedIndexingError):
    code = "provider_output_missing"


class HostedIndexingDenied(HostedIndexingError):
    code = "usage_denied"

    def __init__(self, *, operation: str, reservation: UsageReservation) -> None:
        self.operation = operation
        self.reservation = reservation
        super().__init__(reservation.decision.message or reservation.decision.reason)


TranscriptFetcher = Callable[[str, Source, Job], TranscriptFetchResult]
VideoMetadataFetcher = Callable[[str, Source, Job], HostedVideoInput]
GeminiCleaner = Callable[[NormalizedTranscript, HostedVideoInput, ProviderCallContext], NormalizedTranscript]
VoyageEmbedder = Callable[[Sequence[TranscriptChunkInput], HostedVideoInput, ProviderCallContext], list[list[float]]]


def source_from_public_youtube_input(
    *,
    workspace_id: str,
    source_id: str,
    value: str,
    import_source: Literal["public_api", "public_scrape", "yt_dlp", "manual_url", "manual", "cli"] = "manual_url",
    display_name: str | None = None,
) -> Source:
    """Create a public hosted source from a YouTube URL, video id, handle, or bare handle text."""

    stripped = value.strip()
    if not stripped:
        raise ValueError("source input is required")

    if video_id := extract_public_youtube_video_id(stripped):
        return Source(
            id=source_id,
            workspace_id=workspace_id,
            source_type="video",
            source_url=f"https://www.youtube.com/watch?v={video_id}",
            canonical_video_id=video_id,
            display_name=display_name,
            import_source=import_source,
        )

    if playlist_id := _extract_playlist_id(stripped):
        parsed = urlsplit(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
        url = stripped if parsed.netloc else f"https://www.youtube.com/playlist?list={playlist_id}"
        return Source(
            id=source_id,
            workspace_id=workspace_id,
            source_type="playlist",
            source_url=url,
            canonical_playlist_id=playlist_id,
            display_name=display_name or playlist_id,
            import_source=import_source,
        )

    if channel_id := _extract_channel_id(stripped):
        return Source(
            id=source_id,
            workspace_id=workspace_id,
            source_type="channel",
            source_url=f"https://www.youtube.com/channel/{channel_id}",
            canonical_channel_id=channel_id,
            display_name=display_name or channel_id,
            import_source=import_source,
        )

    handle = _extract_handle(stripped)
    if handle:
        return Source(
            id=source_id,
            workspace_id=workspace_id,
            source_type="handle",
            source_url=f"https://www.youtube.com/@{handle}",
            display_name=display_name or f"@{handle}",
            import_source=import_source,
        )

    parsed = urlsplit(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
    if _is_youtube_host(parsed.netloc):
        return Source(
            id=source_id,
            workspace_id=workspace_id,
            source_type="url",
            source_url=stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}",
            display_name=display_name,
            import_source=import_source,
        )

    raise ValueError(f"not a supported public YouTube source: {value}")


def extract_public_youtube_video_id(value: str) -> str | None:
    stripped = value.strip()
    if YOUTUBE_VIDEO_ID_RE.fullmatch(stripped):
        return stripped
    parsed = urlsplit(stripped if "://" in stripped else f"https://www.youtube.com/{stripped.lstrip('/')}")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
        return candidate if YOUTUBE_VIDEO_ID_RE.fullmatch(candidate) else None
    if not _is_youtube_host(host):
        return None
    query = parse_qs(parsed.query)
    if video_id := query.get("v", [None])[0]:
        return video_id if YOUTUBE_VIDEO_ID_RE.fullmatch(video_id) else None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1] if YOUTUBE_VIDEO_ID_RE.fullmatch(parts[1]) else None
    return None


def plan_mock_hosted_public_indexing(
    *,
    source: Source,
    job: Job,
    video: HostedVideoInput,
    chunks: Sequence[TranscriptChunkInput],
    index_profile: IndexProfileInput | None = None,
    allocations: Sequence[Allocation] | None = None,
    policy: EntitlementPolicy | None = None,
    balance: WorkspaceBalance | None = None,
    gate: UsageGate | None = None,
    search_query: str | None = None,
) -> HostedIndexingPlan:
    """Build the first hosted public indexing smoke plan as pure data and SQL."""

    _validate_public_indexing_inputs(source=source, job=job, video=video, chunks=chunks)
    profile = index_profile or _default_mock_index_profile(source.workspace_id)
    validate_supported_embedding_profile(
        backend=profile.backend,
        embedding_model=profile.embedding_model,
        embedding_dimension=profile.embedding_dimension,
    )
    hosted_video_id = _stable_id("vid", source.workspace_id, video.youtube_video_id)
    normalized_chunks = tuple(sorted(chunks, key=lambda chunk: chunk.chunk_index))
    content_hash = input_hash(
        {
            "video": video.youtube_video_id,
            "language_code": "en",
            "chunks": [_chunk_hash_payload(chunk) for chunk in normalized_chunks],
        }
    )
    transcript_version_id = _stable_id("tx", source.workspace_id, video.youtube_video_id, content_hash)
    embedding_vectors = [
        mock_embedding_vector(chunk.text, dimension=profile.embedding_dimension, salt=f"{video.youtube_video_id}:{chunk.chunk_index}")
        for chunk in normalized_chunks
    ]

    active_allocations = tuple(allocations or _default_allocations(source.workspace_id, profile.id))
    active_policy = policy or EntitlementPolicy(id=f"policy_{source.workspace_id}", workspace_id=source.workspace_id)
    active_balance = balance or WorkspaceBalance(workspace_id=source.workspace_id)
    active_gate = gate or UsageGate()

    usage_reservations = (
        _reserve_usage(
            workspace_id=source.workspace_id,
            subject="voyage",
            operation="embed_documents",
            estimated_units={
                "total_tokens": float(sum(_token_estimate(chunk.text) for chunk in normalized_chunks)),
                "vectors": float(len(normalized_chunks)),
            },
            allocations=active_allocations,
            policy=active_policy,
            balance=active_balance,
            gate=active_gate,
            subject_id=video.youtube_video_id,
            input_payload={"chunks": [_chunk_hash_payload(chunk) for chunk in normalized_chunks], "profile": profile.id},
            extras=[profile.id],
            created_at=job.created_at,
        ),
        _reserve_usage(
            workspace_id=source.workspace_id,
            subject="search_store",
            operation="index_write",
            estimated_units={
                "transcript_versions": 1.0,
                "chunks": float(len(normalized_chunks)),
                "embeddings": float(len(embedding_vectors)),
            },
            allocations=active_allocations,
            policy=active_policy,
            balance=active_balance,
            gate=active_gate,
            subject_id=video.youtube_video_id,
            input_payload={"content_hash": content_hash, "profile": profile.id},
            extras=[profile.id],
            created_at=job.created_at,
        ),
        _reserve_usage(
            workspace_id=source.workspace_id,
            subject="search_store",
            operation="hybrid_query",
            estimated_units={
                "queries": 1.0,
                "candidate_limit": 12.0,
                "query_vector_dimensions": float(profile.embedding_dimension),
            },
            allocations=active_allocations,
            policy=active_policy,
            balance=active_balance,
            gate=active_gate,
            subject_id=video.youtube_video_id,
            input_payload={"query": search_query or _default_search_query(normalized_chunks), "profile": profile.id},
            extras=[profile.id],
            created_at=job.created_at,
        ),
    )

    job_operations = tuple(
        _job_operation(
            workspace_id=source.workspace_id,
            job_id=job.id,
            source_id=source.id,
            video_id=video.youtube_video_id,
            operation=reservation.operation_key,
            input_payload={
                "video": video.youtube_video_id,
                "content_hash": content_hash,
                "reservation_key": reservation.idempotency_key,
            },
            idempotency_extras=[profile.id],
            usage_reservation_id=reservation.id,
            created_at=job.created_at,
        )
        for reservation in usage_reservations
    )

    search_plan = hybrid_query_plan(
        workspace_id=source.workspace_id,
        query=search_query or _default_search_query(normalized_chunks),
        query_vector=mock_embedding_vector(search_query or _default_search_query(normalized_chunks), dimension=profile.embedding_dimension),
        limit=3,
        candidate_multiplier=4,
        index_profile_ref=profile.id,
    )

    sql_operations: list[PlannedSqlOperation] = [
        PlannedSqlOperation(name="videos.upsert", statement=upsert_video_sql(source, video, hosted_video_id=hosted_video_id)),
        PlannedSqlOperation(name="search_index_profiles.upsert", statement=upsert_index_profile_sql(source.workspace_id, profile)),
    ]
    sql_operations.extend(
        PlannedSqlOperation(
            name=f"usage_reservations.{reservation.operation_key}",
            statement=upsert_usage_reservation_sql(reservation),
            idempotency_key=reservation.idempotency_key,
        )
        for reservation in usage_reservations
    )
    sql_operations.extend(
        PlannedSqlOperation(
            name=f"job_operations.{operation.operation}",
            statement=upsert_job_operation_sql(operation),
            operation_id=operation.id,
            idempotency_key=operation.idempotency_key,
        )
        for operation in job_operations
    )
    transcript_metadata = {"job_id": job.id, "source_id": source.id, "youtube_video_id": video.youtube_video_id}
    sql_operations.append(
        PlannedSqlOperation(
            name="transcript_versions.upsert_replacement",
            statement=upsert_transcript_version_sql(
                workspace_id=source.workspace_id,
                video_id=hosted_video_id,
                transcript_version_id=transcript_version_id,
                source="mock_hosted_transcript",
                language_code="en",
                content_hash=content_hash,
                metadata=transcript_metadata,
            ),
            idempotency_key=job_operations[1].idempotency_key,
        )
    )
    for chunk, vector in zip(normalized_chunks, embedding_vectors):
        chunk_id = _stable_id("chk", source.workspace_id, transcript_version_id, str(chunk.chunk_index))
        sql_operations.append(
            PlannedSqlOperation(
                name="chunks.upsert",
                statement=upsert_chunk_sql(
                    workspace_id=source.workspace_id,
                    hosted_video_id=hosted_video_id,
                    transcript_version_id=transcript_version_id,
                    index_profile_id=profile.id,
                    tokenizer=profile.tokenizer,
                    chunk=chunk,
                    chunk_id=chunk_id,
                ),
                idempotency_key=job_operations[1].idempotency_key,
            )
        )
        sql_operations.append(
            PlannedSqlOperation(
                name="chunk_embeddings.upsert",
                statement=upsert_chunk_embedding_sql(
                    workspace_id=source.workspace_id,
                    chunk_id=chunk_id,
                    index_profile_id=profile.id,
                    embedding=vector,
                    embedding_id=_stable_id("emb", source.workspace_id, chunk_id, profile.id),
                    usage_reservation_id=usage_reservations[0].id,
                ),
                idempotency_key=job_operations[0].idempotency_key,
            )
        )
    sql_operations.append(
        PlannedSqlOperation(
            name="search_store.replace_active_transcript",
            statement=replace_active_transcript_sql(
                workspace_id=source.workspace_id,
                video_id=hosted_video_id,
                transcript_version_id=transcript_version_id,
                source="mock_hosted_transcript",
                language_code="en",
                content_hash=content_hash,
                metadata=transcript_metadata,
            ),
            idempotency_key=job_operations[1].idempotency_key,
        )
    )

    return HostedIndexingPlan(
        source=source,
        job=job,
        video=video,
        hosted_video_id=hosted_video_id,
        transcript_version_id=transcript_version_id,
        transcript_content_hash=content_hash,
        index_profile=profile,
        job_operations=job_operations,
        usage_reservations=usage_reservations,
        sql_operations=tuple(sql_operations),
        search_operations=(search_plan,),
    )


def plan_real_hosted_public_indexing(
    *,
    source: Source,
    job: Job,
    video: HostedVideoInput,
    chunks: Sequence[TranscriptChunkInput],
    embedding_vectors: Sequence[Sequence[float]],
    index_profile: IndexProfileInput | None = None,
    transcript_source: str,
    language_code: str | None,
    transcript_metadata: Mapping[str, Any] | None = None,
    embedding_usage_reservation_id: str | None = None,
) -> HostedIndexingPlan:
    """Build replay-safe hosted write operations for a real transcript and embeddings."""

    _validate_public_indexing_inputs(source=source, job=job, video=video, chunks=chunks)
    profile = index_profile or _default_real_index_profile(source.workspace_id)
    validate_supported_embedding_profile(
        backend=profile.backend,
        embedding_model=profile.embedding_model,
        embedding_dimension=profile.embedding_dimension,
    )
    normalized_chunks = tuple(sorted(chunks, key=lambda chunk: chunk.chunk_index))
    if len(normalized_chunks) != len(embedding_vectors):
        raise ValueError("embedding vector count must match chunk count")
    for vector in embedding_vectors:
        if len(vector) != profile.embedding_dimension:
            raise ValueError(f"embedding vector dimension must be {profile.embedding_dimension}")

    hosted_video_id = _stable_id("vid", source.workspace_id, video.youtube_video_id)
    content_hash = input_hash(
        {
            "video": video.youtube_video_id,
            "language_code": language_code,
            "source": transcript_source,
            "index_profile": _index_profile_identity(profile),
            "chunks": [_chunk_hash_payload(chunk) for chunk in normalized_chunks],
        }
    )
    transcript_version_id = _stable_id("tx", source.workspace_id, video.youtube_video_id, content_hash)
    metadata = {
        "job_id": job.id,
        "source_id": source.id,
        "youtube_video_id": video.youtube_video_id,
        "chunking_version": profile.chunking_version,
        **dict(transcript_metadata or {}),
    }

    sql_operations: list[PlannedSqlOperation] = [
        PlannedSqlOperation(name="videos.upsert", statement=upsert_video_sql(source, video, hosted_video_id=hosted_video_id)),
        PlannedSqlOperation(name="search_index_profiles.upsert", statement=upsert_index_profile_sql(source.workspace_id, profile)),
        PlannedSqlOperation(
            name="transcript_versions.upsert_replacement",
            statement=upsert_transcript_version_sql(
                workspace_id=source.workspace_id,
                video_id=hosted_video_id,
                transcript_version_id=transcript_version_id,
                source=transcript_source,
                language_code=language_code,
                content_hash=content_hash,
                metadata=metadata,
            ),
        ),
    ]
    for chunk, vector in zip(normalized_chunks, embedding_vectors, strict=True):
        chunk_id = _stable_id("chk", source.workspace_id, transcript_version_id, str(chunk.chunk_index))
        sql_operations.append(
            PlannedSqlOperation(
                name="chunks.upsert",
                statement=upsert_chunk_sql(
                    workspace_id=source.workspace_id,
                    hosted_video_id=hosted_video_id,
                    transcript_version_id=transcript_version_id,
                    index_profile_id=profile.id,
                    tokenizer=profile.tokenizer,
                    chunk=chunk,
                    chunk_id=chunk_id,
                ),
            )
        )
        sql_operations.append(
            PlannedSqlOperation(
                name="chunk_embeddings.upsert",
                statement=upsert_chunk_embedding_sql(
                    workspace_id=source.workspace_id,
                    chunk_id=chunk_id,
                    index_profile_id=profile.id,
                    embedding=vector,
                    embedding_id=_stable_id("emb", source.workspace_id, chunk_id, profile.id),
                    usage_reservation_id=embedding_usage_reservation_id or "",
                ),
            )
        )
    sql_operations.append(
        PlannedSqlOperation(
            name="search_store.replace_active_transcript",
            statement=replace_active_transcript_sql(
                workspace_id=source.workspace_id,
                video_id=hosted_video_id,
                transcript_version_id=transcript_version_id,
                source=transcript_source,
                language_code=language_code,
                content_hash=content_hash,
                metadata=metadata,
            ),
        )
    )

    return HostedIndexingPlan(
        source=source,
        job=job,
        video=video,
        hosted_video_id=hosted_video_id,
        transcript_version_id=transcript_version_id,
        transcript_content_hash=content_hash,
        index_profile=profile,
        job_operations=(),
        usage_reservations=(),
        sql_operations=tuple(sql_operations),
        search_operations=(),
    )


class HostedIndexingExecutor:
    """Execute hosted public YouTube ingest jobs against hosted providers and Postgres."""

    def __init__(
        self,
        *,
        connection: Any,
        config: AppConfig,
        gate: UsageGate | None = None,
        ledger: Any | None = None,
        usage_context_provider: Any | None = None,
        metadata_fetcher: VideoMetadataFetcher | None = None,
        transcript_fetcher: TranscriptFetcher | None = None,
        gemini_cleaner: GeminiCleaner | None = None,
        voyage_embedder: VoyageEmbedder | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.connection = connection
        self.config = config
        self.gate = gate or PostgresUsageGate(connection)
        self.ledger = ledger or PostgresUsageLedger(connection)
        self.usage_context_provider = usage_context_provider or PostgresUsageContextProvider(connection)
        self.metadata_fetcher = metadata_fetcher or self._fetch_video_metadata
        self.transcript_fetcher = transcript_fetcher or self._fetch_transcript
        self.gemini_cleaner = gemini_cleaner or self._clean_with_gemini
        self.voyage_embedder = voyage_embedder or self._embed_with_voyage
        self.cwd = cwd or Path.cwd()

    def _require_hosted_webshare_proxy(self, *, operation: str) -> ProxyConfig:
        proxy = self.config.proxy
        if proxy.enabled and proxy.kind == "webshare" and proxy.webshare_username and proxy.webshare_password:
            return proxy
        raise HostedWebshareRequired(
            f"Hosted YouTube {operation} requires Webshare residential proxy credentials. "
            "Set YUTOME_WEBSHARE_USERNAME and YUTOME_WEBSHARE_PASSWORD for the hosted worker."
        )

    def execute(
        self,
        job: Job,
        *,
        lease_owner: str,
        now: Any | None = None,
        lease_seconds: int = 900,
    ) -> HostedIndexingExecutionResult:
        from datetime import datetime, timezone
        from yutome.hosted.jobs import retry_job_sql, update_job_operation_status_sql, update_job_status_sql

        fixed_now = now

        def current_time() -> Any:
            return fixed_now or datetime.now(timezone.utc)

        clock = current_time()
        source_id = job.source_id or ""
        video_id: str | None = None
        current_operation_id: str | None = None
        current_operation_name: str | None = None
        current_operation_reservation_id: str | None = None
        current_operation_reservation: UsageReservation | None = None
        try:
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="preparing", now=clock))
            source = self._load_source(job)
            video_id = (
                _optional_str(job.metadata_jsonb.get("youtube_video_id"))
                or source.canonical_video_id
                or extract_public_youtube_video_id(source.source_url)
            )
            if not video_id:
                raise HostedIndexingError("index_video jobs require a concrete public YouTube video id")
            self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            video = self.metadata_fetcher(video_id, source, job)
            transcript_job = job.model_copy(
                update={
                    "metadata_jsonb": {
                        **job.metadata_jsonb,
                        "duration_seconds": video.duration_seconds,
                    }
                }
            )
            self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            transcript_result = self.transcript_fetcher(video.youtube_video_id, source, transcript_job)
            transcript = normalize_transcript(
                video_id=video.youtube_video_id,
                raw_snippets=transcript_result.raw_snippets,
                source=transcript_result.source,
                language=transcript_result.language,
                is_generated=transcript_result.is_generated,
            )
            if not transcript.segments:
                raise HostedIndexingError("transcript had no usable text segments")

            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="cleaning", now=clock))
            gemini_success_events: list[UsageEvent] = []
            gemini_context = self._provider_context(
                workspace_id=job.workspace_id,
                subject="gemini",
                operation="cleanup_transcript",
                estimated_units={"total_tokens": float(_token_estimate(" ".join(segment.text for segment in transcript.segments)))},
                subject_id=video.youtube_video_id,
                input_payload={"transcript_version_id": transcript.version_id, "source": transcript.source},
                metadata={"job_id": job.id, "source_id": source.id, "video_id": video.youtube_video_id},
                success_event_sink=gemini_success_events.append,
            )
            current_operation_name = "gemini.cleanup_transcript"
            current_operation_id = self._upsert_operation(job, source, video.youtube_video_id, current_operation_name, gemini_context)
            cached_transcript = self._operation_output(current_operation_id, workspace_id=job.workspace_id)
            if cached_transcript:
                transcript = _transcript_from_output(cached_transcript["transcript"])
            else:
                self._raise_if_provider_success_without_output(
                    workspace_id=job.workspace_id,
                    idempotency_key=gemini_context.idempotency_key,
                )
                self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
                transcript = self.gemini_cleaner(transcript, video, gemini_context)
                cached_transcript = {"transcript": _transcript_to_output(transcript)}
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._complete_provider_operation_success(
                complete_job_operation_success_sql(
                    workspace_id=job.workspace_id,
                    operation_id=current_operation_id,
                    output=cached_transcript,
                    now=clock,
                    job_id=job.id,
                    lease_owner=lease_owner,
                ),
                success_events=gemini_success_events,
                lost_lease_message="job lease expired before recording Gemini cleanup success",
            )
            current_operation_id = None
            current_operation_name = None

            chunks = _chunks_from_normalized_transcript(transcript)
            if not chunks:
                raise HostedIndexingError("chunking produced no indexable chunks")
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="embedding", now=clock))
            voyage_success_events: list[UsageEvent] = []
            voyage_context = self._provider_context(
                workspace_id=job.workspace_id,
                subject="voyage",
                operation="embed_documents",
                estimated_units={
                    "total_tokens": float(sum(_token_estimate(chunk.text) for chunk in chunks)),
                    "vectors": float(len(chunks)),
                },
                subject_id=video.youtube_video_id,
                input_payload={"transcript_version_id": transcript.version_id, "chunks": [_chunk_hash_payload(chunk) for chunk in chunks]},
                metadata={"job_id": job.id, "source_id": source.id, "video_id": video.youtube_video_id},
                success_event_sink=voyage_success_events.append,
            )
            current_operation_name = "voyage.embed_documents"
            current_operation_id = self._upsert_operation(job, source, video.youtube_video_id, current_operation_name, voyage_context)
            cached_vectors = self._operation_output(current_operation_id, workspace_id=job.workspace_id)
            if cached_vectors:
                vectors = _vectors_from_output(cached_vectors["vectors"])
            else:
                self._raise_if_provider_success_without_output(
                    workspace_id=job.workspace_id,
                    idempotency_key=voyage_context.idempotency_key,
                )
                self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
                vectors = self.voyage_embedder(chunks, video, voyage_context)
                cached_vectors = {"vectors": vectors, "chunk_hashes": [_chunk_hash_payload(chunk) for chunk in chunks]}
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._complete_provider_operation_success(
                complete_job_operation_success_sql(
                    workspace_id=job.workspace_id,
                    operation_id=current_operation_id,
                    output=cached_vectors,
                    now=clock,
                    job_id=job.id,
                    lease_owner=lease_owner,
                ),
                success_events=voyage_success_events,
                lost_lease_message="job lease expired before recording Voyage embedding success",
            )
            current_operation_id = None
            current_operation_name = None

            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="writing_index", now=clock))
            current_operation_name = "search_store.index_write"
            search_reservation = self._reserve_search_write(job=job, source=source, video=video, chunks=chunks, vectors=vectors)
            current_operation_reservation_id = search_reservation.id
            current_operation_reservation = search_reservation
            current_operation_id = self._upsert_raw_operation(
                job,
                source,
                video.youtube_video_id,
                current_operation_name,
                {"chunks": [_chunk_hash_payload(chunk) for chunk in chunks], "vectors": len(vectors)},
                usage_reservation_id=search_reservation.id,
            )
            if not search_reservation.decision.allowed:
                self._append_denied_usage_event(search_reservation)
                raise HostedIndexingDenied(operation="search_store.index_write", reservation=search_reservation)
            plan = plan_real_hosted_public_indexing(
                source=source,
                job=job,
                video=video,
                chunks=chunks,
                embedding_vectors=vectors,
                transcript_source=transcript.source,
                language_code=transcript.language,
                transcript_metadata={
                    "is_generated": transcript.is_generated,
                    "text_hash": transcript.text_hash,
                    "raw_source": transcript_result.source,
                    "search_store_usage_reservation_id": search_reservation.id,
                },
                embedding_usage_reservation_id=stable_usage_reservation_id(
                    workspace_id=job.workspace_id,
                    idempotency_key=voyage_context.idempotency_key,
                ),
            )
            with self._transaction():
                self._assert_active_job_lease(job, lease_owner=lease_owner, now=current_time())
                for operation in plan.sql_operations:
                    self._execute_statement(operation.statement)
            self._append_search_write_success(search_reservation, chunks=chunks, vectors=vectors, video=video)
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_required_statement(
                update_job_operation_status_sql(
                    operation_id=current_operation_id,
                    workspace_id=job.workspace_id,
                    status="succeeded",
                    now=clock,
                    usage_reservation_id=current_operation_reservation_id,
                    job_id=job.id,
                    lease_owner=lease_owner,
                ),
                lost_lease_message="job lease expired before recording search-store index write success",
            )
            current_operation_id = None
            current_operation_name = None
            current_operation_reservation_id = None
            current_operation_reservation = None
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="succeeded", now=clock))
            return HostedIndexingExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source.id,
                youtube_video_id=video.youtube_video_id,
                status="succeeded",
                hosted_video_id=plan.hosted_video_id,
                transcript_version_id=plan.transcript_version_id,
                chunks_written=len(chunks),
                embeddings_written=len(vectors),
            )
        except UsageReservationDenied as exc:
            if current_operation_id is not None:
                self._execute_statement(
                    update_job_operation_status_sql(
                        operation_id=current_operation_id,
                        workspace_id=job.workspace_id,
                        status="denied",
                        now=clock,
                        error_code=exc.reservation.decision.reason,
                        error_message=exc.reservation.decision.message,
                        usage_reservation_id=exc.reservation.id,
                        job_id=job.id,
                        lease_owner=lease_owner,
                    )
                )
            self._execute_statement(
                update_job_status_sql(
                    job_id=job.id,
                    lease_owner=lease_owner,
                    status="denied",
                    now=clock,
                    error_code=exc.reservation.decision.reason,
                    error_message=exc.reservation.decision.message,
                )
            )
            return HostedIndexingExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source_id,
                youtube_video_id=video_id,
                status="denied",
                denied_operation=current_operation_name or f"{exc.reservation.subject}.{exc.reservation.operation}",
                error_code=exc.reservation.decision.reason,
                error_message=exc.reservation.decision.message,
            )
        except HostedIndexingDenied as exc:
            if current_operation_id is not None:
                self._execute_statement(
                    update_job_operation_status_sql(
                        operation_id=current_operation_id,
                        workspace_id=job.workspace_id,
                        status="denied",
                        now=clock,
                        error_code=exc.reservation.decision.reason,
                        error_message=exc.reservation.decision.message,
                        usage_reservation_id=exc.reservation.id,
                        job_id=job.id,
                        lease_owner=lease_owner,
                    )
                )
            self._execute_statement(
                update_job_status_sql(
                    job_id=job.id,
                    lease_owner=lease_owner,
                    status="denied",
                    now=clock,
                    error_code=exc.reservation.decision.reason,
                    error_message=exc.reservation.decision.message,
                )
            )
            return HostedIndexingExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source_id,
                youtube_video_id=video_id,
                status="denied",
                denied_operation=exc.operation,
                error_code=exc.reservation.decision.reason,
                error_message=exc.reservation.decision.message,
            )
        except Exception as exc:
            error_message = redact_sensitive_failure_text(str(exc))
            retryable = (
                not isinstance(exc, (HostedIndexingLostLease, HostedProviderOutputMissing))
                and (
                    is_youtube_block_error(exc)
                    or is_proxy_payment_error(exc)
                    or _looks_retryable_discovery_error(exc)
                    or _looks_retryable_indexing_error(exc)
                )
            )
            if current_operation_reservation is not None and current_operation_reservation.decision.allowed:
                self._append_search_write_failure(
                    current_operation_reservation,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error_message=error_message,
                )
            if current_operation_id is not None:
                self._execute_statement(
                    update_job_operation_status_sql(
                        operation_id=current_operation_id,
                        workspace_id=job.workspace_id,
                        status="failed_retryable" if retryable else "failed_final",
                        now=clock,
                        error_code=type(exc).__name__,
                        error_message=error_message,
                        usage_reservation_id=current_operation_reservation_id,
                        job_id=job.id,
                        lease_owner=lease_owner,
                    )
                )
            if retryable:
                self._execute_statement(
                    retry_job_sql(
                        job_id=job.id,
                        lease_owner=lease_owner,
                        now=clock,
                        retry_after=clock + timedelta(seconds=300),
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=error_message,
                    )
                )
                result_status: Literal["failed", "retry_wait"] = "retry_wait"
            else:
                self._execute_statement(
                    update_job_status_sql(
                        job_id=job.id,
                        lease_owner=lease_owner,
                        status="failed",
                        now=clock,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=error_message,
                    )
                )
                result_status = "failed"
            return HostedIndexingExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source_id,
                youtube_video_id=video_id,
                status=result_status,
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=error_message,
            )

    def _fetch_video_metadata(self, video_id: str, source: Source, job: Job) -> HostedVideoInput:
        proxy = self._require_hosted_webshare_proxy(operation="metadata fetch")
        metadata = {"job_id": job.id, "source_id": source.id, "video_id": video_id, "phase": "metadata_fetch"}
        youtube_context = self._youtube_fetch_context(
            workspace_id=job.workspace_id,
            subject_id=video_id,
            operation="metadata_fetch",
            source="yt-dlp.metadata",
            metadata=metadata,
        )
        webshare_context = self._webshare_proxy_context(
            workspace_id=job.workspace_id,
            subject_id=video_id,
            source="yt-dlp.metadata",
            bytes_estimate=1_000_000,
            metadata=metadata,
        )

        def call() -> DiscoveredVideo:
            return discover_video(
                target=video_id,
                cwd=self.cwd,
                proxy=proxy,
                ytdlp_config=self.config.yt_dlp,
                hosted_context=webshare_context,
            )

        discovered = execute_provider_call(
            youtube_context,
            call,
            normalize_usage=lambda result: UsageNormalization(
                subject="youtube",
                operation="metadata_fetch",
                actual_units={"request_count": 1},
                raw_usage={"duration_seconds": result.duration_seconds},
                metadata={"fetch_source": "yt-dlp.metadata"},
            ),
        )
        raw = discovered.raw or {}
        duration_seconds = discovered.duration_seconds
        if duration_seconds is None:
            duration_seconds = _int_or_none(raw.get("duration"))
        return HostedVideoInput(
            youtube_video_id=discovered.video_id,
            title=discovered.title or source.display_name or discovered.video_id,
            url=discovered.url,
            channel_id=discovered.channel_id,
            description=_text_or_none(raw.get("description")) or "",
            duration_seconds=duration_seconds,
            published_at=_hosted_ytdlp_published_at(raw),
            metadata=_hosted_ytdlp_metadata(discovered),
        )

    def _fetch_transcript(self, video_id: str, source: Source, job: Job) -> TranscriptFetchResult:
        proxy = self._require_hosted_webshare_proxy(operation="transcript fetch")
        metadata = {"job_id": job.id, "source_id": source.id, "video_id": video_id, "phase": "transcript_fetch"}
        youtube_context = self._youtube_fetch_context(
            workspace_id=job.workspace_id,
            subject_id=video_id,
            operation="transcript_fetch",
            source="transcript.fetch",
            metadata=metadata,
        )
        webshare_context = self._webshare_proxy_context(
            workspace_id=job.workspace_id,
            subject_id=video_id,
            source="transcript.fetch",
            bytes_estimate=2_000_000,
            metadata=metadata,
        )
        try:
            def call() -> TranscriptFetchResult:
                if self.config.transcripts.prefer_ytdlp_subtitles:
                    return fetch_subtitle_transcript_with_ytdlp(
                        video_id=video_id,
                        cwd=self.cwd,
                        language=self.config.transcripts.preferred_languages[0],
                        proxy=proxy,
                        ytdlp_config=self.config.yt_dlp,
                        allow_translated_captions=self.config.transcripts.allow_translated_captions,
                        hosted_context=webshare_context,
                    )
                return fetch_transcript(
                    video_id=video_id,
                    languages=self.config.transcripts.preferred_languages,
                    proxy=proxy,
                    timeout_seconds=self.config.transcripts.request_timeout_seconds,
                    hosted_context=webshare_context,
                )

            return execute_provider_call(
                youtube_context,
                call,
                normalize_usage=lambda result: UsageNormalization(
                    subject="youtube",
                    operation="transcript_fetch",
                    actual_units={"request_count": 1, "transcript_segments": len(result.raw_snippets)},
                    raw_usage={"source": result.source, "language": result.language, "is_generated": result.is_generated},
                    metadata={"fetch_source": result.source},
                ),
            )
        except UsageReservationDenied:
            raise
        except Exception:
            if not self.config.gemini.fallback_enabled:
                raise
            duration_seconds = _duration_seconds_from_job(job)
            fallback_context = self._provider_context(
                workspace_id=job.workspace_id,
                subject="gemini",
                operation="transcribe_media",
                estimated_units={
                    "media_seconds": max(1, int(duration_seconds or 0)),
                    "total_tokens": max(1, int((duration_seconds or self.config.gemini.window_seconds) / 60) * 750),
                },
                subject_id=video_id,
                input_payload={
                    "video_id": video_id,
                    "duration_seconds": duration_seconds,
                    "media_resolution": self.config.gemini.media_resolution,
                    "window_seconds": self.config.gemini.window_seconds,
                },
                metadata={"job_id": job.id, "source_id": source.id, "video_id": video_id, "phase": "fallback_transcription"},
            )
            return transcribe_youtube_url_with_gemini(
                video_id=video_id,
                config=self.config.gemini,
                duration_seconds=duration_seconds,
                hosted_context=fallback_context,
            )

    def _clean_with_gemini(
        self,
        transcript: NormalizedTranscript,
        video: HostedVideoInput,
        context: ProviderCallContext,
    ) -> NormalizedTranscript:
        cleaned, _stats = cleanup_transcript_with_gemini(
            transcript,
            config=self.config.gemini,
            context=TranscriptCleanupContext(
                video_title=video.title,
                video_description=video.description,
                channel_title=str((video.metadata or {}).get("channel_title") or "") or None,
                channel_handle=str((video.metadata or {}).get("channel_handle") or "") or None,
            ),
            batch_segments=self.config.transcript_cleanup.batch_segments,
            concurrency=self.config.transcript_cleanup.concurrency,
            max_change_ratio=self.config.transcript_cleanup.max_change_ratio,
            max_patch_retries=self.config.transcript_cleanup.max_patch_retries,
            hosted_context=context,
        )
        return cleaned

    def _embed_with_voyage(
        self,
        chunks: Sequence[TranscriptChunkInput],
        video: HostedVideoInput,
        context: ProviderCallContext,
    ) -> list[list[float]]:
        def call() -> Any:
            import voyageai

            return voyageai.Client().embed(
                [chunk.text for chunk in chunks],
                model=DEFAULT_EMBEDDING_MODEL,
                input_type="document",
                output_dimension=DEFAULT_EMBEDDING_DIMENSION,
            )

        response = execute_provider_call(
            context,
            call,
            normalize_usage=lambda result: UsageNormalization(
                subject="voyage",
                operation="embed_documents",
                actual_units=dict(getattr(result, "usage", {}) or {}),
                raw_usage={"usage": dict(getattr(result, "usage", {}) or {})},
            ),
        )
        return [list(vector) for vector in response.embeddings]

    def _provider_context(
        self,
        *,
        workspace_id: str,
        subject: UsageSubject,
        operation: str,
        estimated_units: Mapping[str, Any],
        subject_id: str,
        input_payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
        success_event_sink: Callable[[UsageEvent], None] | None = None,
    ) -> ProviderCallContext:
        usage_context = self._usage_context(
            workspace_id=workspace_id,
            subject=subject,
            operation=operation,
            estimated_units=estimated_units,
        )
        operation_input_hash = input_hash({"subject": subject, "operation": operation, "input": input_payload})
        reservation_key = job_operation_idempotency_key(
            workspace_id=workspace_id,
            operation=f"{subject}.{operation}",
            input_hash_value=operation_input_hash,
            video_id=subject_id,
            extras=_real_index_profile_extras(workspace_id),
        )
        return ProviderCallContext(
            gate=self.gate,
            ledger=self.ledger,
            workspace_id=workspace_id,
            subject=subject,
            operation=operation,
            estimated_units=dict(estimated_units),
            allocation=usage_context.allocation,
            policy=usage_context.policy,
            balance=usage_context.balance,
            idempotency_key=reservation_key,
            metadata={**dict(metadata), "input_hash": operation_input_hash},
            success_event_sink=success_event_sink,
        )

    def _usage_context(
        self,
        *,
        workspace_id: str,
        subject: UsageSubject,
        operation: str,
        estimated_units: Mapping[str, Any],
    ) -> HostedMcpUsageContext:
        auth = HostedMcpAuthContext(workspace_id=workspace_id).validated()
        provider = getattr(self.usage_context_provider, "for_subject", None)
        if callable(provider):
            return provider(auth=auth, subject=subject, operation=operation, estimated_units=estimated_units)
        return self.usage_context_provider(auth, operation, estimated_units)

    def _youtube_fetch_context(
        self,
        *,
        workspace_id: str,
        subject_id: str,
        operation: str,
        source: str,
        metadata: Mapping[str, Any],
    ) -> ProviderCallContext:
        return self._provider_context(
            workspace_id=workspace_id,
            subject="youtube",
            operation=operation,
            estimated_units={"request_count": 1},
            subject_id=subject_id,
            input_payload={"source": source, "target": subject_id},
            metadata={**dict(metadata), "fetch_source": source},
        )

    def _webshare_proxy_context(
        self,
        *,
        workspace_id: str,
        subject_id: str,
        source: str,
        bytes_estimate: int,
        metadata: Mapping[str, Any],
    ) -> ProviderCallContext | None:
        proxy = self.config.proxy
        if not (
            proxy.enabled
            and proxy.kind == "webshare"
            and proxy.webshare_username
            and proxy.webshare_password
        ):
            return None
        return self._provider_context(
            workspace_id=workspace_id,
            subject="webshare",
            operation="proxy_fetch",
            estimated_units={"request_count": 1, "bytes": bytes_estimate},
            subject_id=subject_id,
            input_payload={"source": source, "target": subject_id, "bytes_estimate": bytes_estimate},
            metadata={**dict(metadata), "proxy_source": source},
        )

    def _reserve_search_write(
        self,
        *,
        job: Job,
        source: Source,
        video: HostedVideoInput,
        chunks: Sequence[TranscriptChunkInput],
        vectors: Sequence[Sequence[float]],
    ) -> UsageReservation:
        estimated_units = {
            "transcript_versions": 1,
            "chunks": len(chunks),
            "embeddings": len(vectors),
        }
        input_payload = {"job_id": job.id, "chunks": [_chunk_hash_payload(chunk) for chunk in chunks]}
        operation_input_hash = input_hash({"subject": "search_store", "operation": "index_write", "input": input_payload})
        reservation_key = job_operation_idempotency_key(
            workspace_id=job.workspace_id,
            operation="search_store.index_write",
            input_hash_value=operation_input_hash,
            video_id=video.youtube_video_id,
            extras=_real_index_profile_extras(job.workspace_id),
        )
        usage_context = self._usage_context(
            workspace_id=job.workspace_id,
            subject="search_store",
            operation="index_write",
            estimated_units=estimated_units,
        )
        reservation = self.gate.reserve(
            workspace_id=job.workspace_id,
            subject="search_store",
            operation="index_write",
            estimated_units=estimated_units,
            allocation=usage_context.allocation,
            policy=usage_context.policy,
            balance=usage_context.balance,
            idempotency_key=reservation_key,
        )
        reservation = reservation.model_copy(
            update={
                "id": stable_usage_reservation_id(workspace_id=job.workspace_id, idempotency_key=reservation_key),
                "created_at": job.created_at,
                "metadata": {
                    **reservation.metadata,
                    "input_hash": operation_input_hash,
                    "idempotency_extras": list(_real_index_profile_extras(job.workspace_id)),
                },
            }
        )
        self._execute_statement(upsert_usage_reservation_sql(reservation))
        return reservation

    def _upsert_operation(
        self,
        job: Job,
        source: Source,
        video_id: str,
        operation_name: str,
        context: ProviderCallContext,
    ) -> str:
        operation = _job_operation(
            workspace_id=job.workspace_id,
            job_id=job.id,
            source_id=source.id,
            video_id=video_id,
            operation=operation_name,
            input_payload={"idempotency_key": context.idempotency_key, "input_hash": context.metadata.get("input_hash")},
            idempotency_extras=_real_index_profile_extras(job.workspace_id),
            usage_reservation_id="",
            created_at=job.created_at,
        )
        self._execute_statement(upsert_job_operation_sql(operation))
        return operation.id

    def _upsert_raw_operation(
        self,
        job: Job,
        source: Source,
        video_id: str,
        operation_name: str,
        input_payload: Mapping[str, Any],
        *,
        usage_reservation_id: str = "",
    ) -> str:
        operation = _job_operation(
            workspace_id=job.workspace_id,
            job_id=job.id,
            source_id=source.id,
            video_id=video_id,
            operation=operation_name,
            input_payload=input_payload,
            idempotency_extras=_real_index_profile_extras(job.workspace_id),
            usage_reservation_id=usage_reservation_id,
            created_at=job.created_at,
        )
        self._execute_statement(upsert_job_operation_sql(operation))
        return operation.id

    def _operation_output(self, operation_id: str, *, workspace_id: str) -> dict[str, Any] | None:
        row = _execute_one(self.connection, job_operation_output_sql(workspace_id=workspace_id, operation_id=operation_id))
        if row is None or row.get("status") in {"denied", "failed_final"}:
            return None
        output = dict(_json_value(row.get("output_json")))
        return output or None

    def _renew_job_lease_or_raise(self, job: Job, *, lease_owner: str, now: Any, lease_seconds: int) -> Any:
        from yutome.hosted.jobs import renew_job_lease_sql

        rows = self._execute_statement(
            renew_job_lease_sql(job_id=job.id, lease_owner=lease_owner, now=now, lease_seconds=lease_seconds)
        )
        if not rows:
            raise HostedIndexingLostLease(f"job lease expired or moved before worker {lease_owner!r} could continue")
        return now

    def _assert_active_job_lease(self, job: Job, *, lease_owner: str, now: Any) -> None:
        from yutome.hosted.jobs import active_job_lease_sql

        rows = self._execute_statement(active_job_lease_sql(job_id=job.id, lease_owner=lease_owner, now=now))
        if not rows:
            raise HostedIndexingLostLease(f"job lease expired or moved before worker {lease_owner!r} could write index data")

    def _execute_required_statement(self, statement: SqlStatement, *, lost_lease_message: str) -> list[dict[str, Any]]:
        rows = self._execute_statement(statement)
        if not rows:
            raise HostedIndexingLostLease(lost_lease_message)
        return rows

    def _complete_provider_operation_success(
        self,
        statement: SqlStatement,
        *,
        success_events: Sequence[UsageEvent],
        lost_lease_message: str,
    ) -> list[dict[str, Any]]:
        if not success_events:
            return self._execute_required_statement(statement, lost_lease_message=lost_lease_message)
        with self._transaction():
            rows = self._execute_required_statement(statement, lost_lease_message=lost_lease_message)
            for event in success_events:
                self.ledger.append(event)
            return rows

    def _raise_if_provider_success_without_output(self, *, workspace_id: str, idempotency_key: str) -> None:
        row = _execute_one(
            self.connection,
            provider_success_usage_event_sql(workspace_id=workspace_id, idempotency_key=idempotency_key),
        )
        if row is not None:
            raise HostedProviderOutputMissing(
                "provider response already succeeded but operation output is missing; refusing to call provider again"
            )

    def _append_denied_usage_event(self, reservation: UsageReservation) -> None:
        event = denied_usage_event(reservation)
        event.metadata = {
            **event.metadata,
            "idempotency_key": reservation.idempotency_key,
            "allocation_id": reservation.allocation_id,
        }
        self.ledger.append(event)

    def _append_search_write_success(
        self,
        reservation: UsageReservation,
        *,
        chunks: Sequence[TranscriptChunkInput],
        vectors: Sequence[Sequence[float]],
        video: HostedVideoInput,
    ) -> None:
        event = usage_event_from_normalization(
            UsageNormalization(
                subject="search_store",
                operation="index_write",
                actual_units={"transcript_versions": 1, "chunks": len(chunks), "embeddings": len(vectors)},
                metadata={
                    "idempotency_key": reservation.idempotency_key,
                    "allocation_id": reservation.allocation_id,
                    "video_id": video.youtube_video_id,
                    "index_profile_id": _default_real_index_profile(reservation.workspace_id).id,
                },
            ),
            reservation=reservation,
            event_type="service_operation_succeeded",
        )
        self.ledger.append(event)

    def _append_search_write_failure(
        self,
        reservation: UsageReservation,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        event = UsageEvent(
            reservation_id=reservation.id,
            workspace_id=reservation.workspace_id,
            subject=reservation.subject,
            operation=reservation.operation,
            event_type="service_operation_failed",
            status="failed",
            error_code=error_code,
            metadata={
                "estimated_units": dict(reservation.estimated_units),
                "idempotency_key": reservation.idempotency_key,
                "allocation_id": reservation.allocation_id,
                "message": redact_sensitive_failure_text(error_message),
            },
        )
        self.ledger.append(event)

    def _load_source(self, job: Job) -> Source:
        if not job.source_id:
            raise HostedIndexingError("index_video job is missing source_id")
        row = _execute_one(
            self.connection,
            SqlStatement(
                sql="""
SELECT *
FROM sources
WHERE id = %(source_id)s
  AND workspace_id = %(workspace_id)s;
""".strip(),
                params={"source_id": job.source_id, "workspace_id": job.workspace_id},
            ),
        )
        if row is None:
            raise HostedIndexingError(f"source not found for job: {job.source_id}")
        return _source_from_row(row)

    def _load_allocations(self, workspace_id: str) -> tuple[Allocation, ...]:
        provider_rows = _execute_rows(
            self.connection,
            SqlStatement(
                sql="SELECT * FROM provider_allocations WHERE workspace_id = %(workspace_id)s AND status <> 'invalid';",
                params={"workspace_id": workspace_id},
            ),
        )
        service_rows = _execute_rows(
            self.connection,
            SqlStatement(
                sql="SELECT * FROM service_allocations WHERE workspace_id = %(workspace_id)s AND status <> 'invalid';",
                params={"workspace_id": workspace_id},
            ),
        )
        allocations: list[Allocation] = []
        for row in provider_rows:
            allocations.append(
                ProviderAllocation(
                    id=str(row["id"]),
                    workspace_id=str(row["workspace_id"]),
                    provider=row["provider"],
                    operation=str(row["operation"]),
                    credential_mode=row["credential_mode"],
                    status=row.get("status", "active"),
                    model_or_plan=row.get("model_or_plan"),
                    external_allocation_id=row.get("external_allocation_id"),
                    metadata=dict(_json_value(row.get("metadata_json"))),
                )
            )
        for row in service_rows:
            from yutome.hosted.models import ServiceAllocation

            allocations.append(
                ServiceAllocation(
                    id=str(row["id"]),
                    workspace_id=str(row["workspace_id"]),
                    service=row["service"],
                    operation=str(row["operation"]),
                    credential_mode=row.get("credential_mode", "service_internal"),
                    status=row.get("status", "active"),
                    backend=str(row["backend"]),
                    index_profile_ref=row.get("index_profile_ref"),
                    metadata=dict(_json_value(row.get("metadata_json"))),
                )
            )
        return tuple(allocations)

    def _load_policy(self, workspace_id: str) -> EntitlementPolicy:
        row = _execute_one(
            self.connection,
            SqlStatement(
                sql="""
SELECT *
FROM entitlement_policies
WHERE workspace_id = %(workspace_id)s
  AND status = 'active'
ORDER BY updated_at DESC
LIMIT 1;
""".strip(),
                params={"workspace_id": workspace_id},
            ),
        )
        if row is None:
            return EntitlementPolicy(id=f"policy_missing_{workspace_id}", workspace_id=workspace_id)
        return EntitlementPolicy(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            allowed_operations=set(row.get("allowed_operations") or ()),
            hard_limits_by_operation=dict(_json_value(row.get("hard_limits_jsonb"))),
        )

    def _load_balance(self, workspace_id: str) -> WorkspaceBalance:
        row = _execute_one(
            self.connection,
            SqlStatement(
                sql="SELECT * FROM workspace_balances WHERE workspace_id = %(workspace_id)s;",
                params={"workspace_id": workspace_id},
            ),
        )
        if row is None:
            return WorkspaceBalance(workspace_id=workspace_id)
        return WorkspaceBalance(
            workspace_id=str(row["workspace_id"]),
            remaining_units=dict(_json_value(row.get("remaining_units_jsonb"))),
            unlimited_units=set(row.get("unlimited_units") or ()),
        )

    def _execute_statement(self, statement: SqlStatement) -> list[dict[str, Any]]:
        return _rows_from_result(self.connection.execute(statement.sql, statement.params))

    @contextmanager
    def _transaction(self):
        transaction = getattr(self.connection, "transaction", None)
        if callable(transaction):
            with transaction():
                yield
            return
        self.connection.execute("BEGIN", {})
        try:
            yield
        except Exception:
            self.connection.execute("ROLLBACK", {})
            raise
        self.connection.execute("COMMIT", {})


SourceVideoDiscoverer = Callable[[Source, ProviderCallContext | None, int | None], Sequence[DiscoveredVideo]]


class HostedSourceDiscoveryExecutor(HostedIndexingExecutor):
    """Execute hosted source discovery jobs and enqueue concrete video ingest work."""

    def __init__(
        self,
        *,
        connection: Any,
        config: AppConfig,
        gate: UsageGate | None = None,
        ledger: Any | None = None,
        usage_context_provider: Any | None = None,
        video_discoverer: SourceVideoDiscoverer | None = None,
        cwd: Path | None = None,
    ) -> None:
        super().__init__(
            connection=connection,
            config=config,
            gate=gate,
            ledger=ledger,
            usage_context_provider=usage_context_provider,
            cwd=cwd,
        )
        self.video_discoverer = video_discoverer or self._discover_public_source_videos

    def execute(
        self,
        job: Job,
        *,
        lease_owner: str,
        now: Any | None = None,
        lease_seconds: int = 900,
    ) -> HostedSourceDiscoveryExecutionResult:
        from datetime import datetime, timezone
        from yutome.hosted.jobs import retry_job_sql, update_job_status_sql

        fixed_now = now

        def current_time() -> Any:
            return fixed_now or datetime.now(timezone.utc)

        clock = current_time()
        source_id = job.source_id or ""
        try:
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="discovering", now=clock))
            source = self._load_source(job)
            decision = source_discovery_decision(source)
            if not decision.discoverable:
                self._execute_statement(
                    update_job_status_sql(
                        job_id=job.id,
                        lease_owner=lease_owner,
                        status="denied",
                        now=clock,
                        error_code=decision.code,
                        error_message=decision.message,
                    )
                )
                return HostedSourceDiscoveryExecutionResult(
                    job_id=job.id,
                    workspace_id=job.workspace_id,
                    source_id=source.id,
                    status="denied",
                    error_code=decision.code,
                    error_message=decision.message,
                )
            max_videos = _positive_int_or_none(job.metadata_jsonb.get("max_new_videos_per_run")) or 25
            proxy_required = source.source_type != "video"
            if proxy_required:
                self._require_hosted_webshare_proxy(operation="source discovery")
            hosted_context = self._webshare_proxy_context(
                workspace_id=job.workspace_id,
                subject_id=source.canonical_ref,
                source="yt-dlp.discovery",
                bytes_estimate=2_000_000,
                metadata={"job_id": job.id, "source_id": source.id, "source_type": source.source_type, "phase": "source_discovery"},
            )
            self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            videos = tuple(self.video_discoverer(source, hosted_context, max_videos))
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="queued_video_jobs", now=clock))
            enqueued = 0
            video_ids: list[str] = []
            for video in videos[:max_videos]:
                if not YOUTUBE_VIDEO_ID_RE.fullmatch(video.video_id):
                    continue
                rows = self._execute_statement(
                    enqueue_index_video_job_sql(
                        workspace_id=job.workspace_id,
                        source_id=source.id,
                        video_id=video.video_id,
                        priority=job.priority,
                        now=clock,
                        metadata={
                            "source_discovery_job_id": job.id,
                            "source_refresh_policy_id": job.metadata_jsonb.get("source_refresh_policy_id"),
                            "playlist_tab": video.playlist_tab,
                            "title": video.title,
                            "channel_id": video.channel_id,
                            "channel_title": video.channel_title,
                            "channel_handle": video.channel_handle,
                            "duration_seconds": video.duration_seconds,
                        },
                    )
                )
                enqueued += 1 if rows else 0
                video_ids.append(video.video_id)
            self._execute_statement(
                finish_source_discovery_sql(
                    workspace_id=job.workspace_id,
                    source_id=source.id,
                    policy_id=_optional_str(job.metadata_jsonb.get("source_refresh_policy_id")),
                    now=clock,
                    discovered_videos=len(videos),
                    enqueued_jobs=enqueued,
                    video_ids=video_ids,
                    lease_owner=lease_owner,
                )
            )
            clock = self._renew_job_lease_or_raise(job, lease_owner=lease_owner, now=current_time(), lease_seconds=lease_seconds)
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="succeeded", now=clock))
            return HostedSourceDiscoveryExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source.id,
                status="succeeded",
                discovered_videos=len(videos),
                enqueued_jobs=enqueued,
                video_ids=tuple(video_ids),
            )
        except UsageReservationDenied as exc:
            self._execute_statement(
                update_job_status_sql(
                    job_id=job.id,
                    lease_owner=lease_owner,
                    status="denied",
                    now=clock,
                    error_code=exc.reservation.decision.reason,
                    error_message=exc.reservation.decision.message,
                )
            )
            return HostedSourceDiscoveryExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source_id,
                status="denied",
                error_code=exc.reservation.decision.reason,
                error_message=exc.reservation.decision.message,
            )
        except Exception as exc:
            error_message = redact_sensitive_failure_text(str(exc))
            retryable = is_youtube_block_error(exc) or is_proxy_payment_error(exc) or _looks_retryable_discovery_error(exc)
            if retryable:
                self._execute_statement(
                    retry_job_sql(
                        job_id=job.id,
                        lease_owner=lease_owner,
                        now=clock,
                        retry_after=clock + timedelta(seconds=300),
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=error_message,
                    )
                )
                status: Literal["failed", "retry_wait"] = "retry_wait"
            else:
                self._execute_statement(
                    update_job_status_sql(
                        job_id=job.id,
                        lease_owner=lease_owner,
                        status="failed",
                        now=clock,
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=error_message,
                    )
                )
                status = "failed"
            self._execute_statement(
                finish_source_discovery_sql(
                    workspace_id=job.workspace_id,
                    source_id=source_id,
                    policy_id=_optional_str(job.metadata_jsonb.get("source_refresh_policy_id")),
                    now=clock,
                    discovered_videos=0,
                    enqueued_jobs=0,
                    video_ids=[],
                    lease_owner=lease_owner,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error_message=error_message,
                )
            )
            return HostedSourceDiscoveryExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source_id,
                status=status,
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=error_message,
            )

    def _discover_public_source_videos(
        self,
        source: Source,
        hosted_context: ProviderCallContext | None,
        limit: int | None,
    ) -> Sequence[DiscoveredVideo]:
        if source.requires_youtube_grant:
            raise HostedIndexingError("OAuth subscription discovery requires a stored YouTube grant executor.")
        if source.source_type == "video":
            video_id = source.canonical_video_id or extract_public_youtube_video_id(source.source_url)
            if not video_id:
                raise HostedIndexingError("video source is missing a canonical YouTube video id")
            return [
                DiscoveredVideo(
                    video_id=video_id,
                    title=source.display_name,
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    channel_id=source.canonical_channel_id,
                    channel_title=None,
                    channel_handle=None,
                    duration_seconds=None,
                    playlist_tab="video",
                    raw={},
                )
            ]
        if source.source_type not in {"channel", "handle", "playlist", "url"}:
            raise HostedIndexingError(f"source discovery is not implemented for source_type={source.source_type}")
        proxy = self._require_hosted_webshare_proxy(operation="source discovery")
        return discover_videos(
            target=source.source_url,
            cwd=self.cwd,
            limit=limit,
            proxy=proxy,
            ytdlp_config=self.config.yt_dlp,
            hosted_context=hosted_context,
        )


def upsert_video_sql(source: Source, video: HostedVideoInput, *, hosted_video_id: str) -> SqlStatement:
    statement = insert(videos).values(
        id=hosted_video_id,
        workspace_id=source.workspace_id,
        source_id=source.id,
        youtube_video_id=video.youtube_video_id,
        channel_id=video.channel_id,
        title=video.title,
        description=video.description,
        published_at=video.published_at,
        duration_seconds=video.duration_seconds,
        metadata_json=_json_param(video.metadata or {}),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[videos.c.workspace_id, videos.c.youtube_video_id],
        set_={
            "source_id": statement.excluded.source_id,
            "channel_id": statement.excluded.channel_id,
            "title": statement.excluded.title,
            "description": statement.excluded.description,
            "published_at": statement.excluded.published_at,
            "duration_seconds": statement.excluded.duration_seconds,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(videos)
    return _sql_statement(statement)


def upsert_index_profile_sql(workspace_id: str, profile: IndexProfileInput) -> SqlStatement:
    statement = insert(search_index_profiles).values(
        id=profile.id,
        workspace_id=workspace_id,
        backend=profile.backend,
        embedding_model=profile.embedding_model,
        embedding_dimension=profile.embedding_dimension,
        chunking_version=profile.chunking_version,
        tokenizer=profile.tokenizer,
        metadata_json=_json_param(profile.metadata or {}),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[search_index_profiles.c.id],
        set_={
            "backend": statement.excluded.backend,
            "embedding_model": statement.excluded.embedding_model,
            "embedding_dimension": statement.excluded.embedding_dimension,
            "chunking_version": statement.excluded.chunking_version,
            "tokenizer": statement.excluded.tokenizer,
            "metadata_json": statement.excluded.metadata_json,
        },
    ).returning(search_index_profiles)
    return _sql_statement(statement)


def upsert_transcript_version_sql(
    *,
    workspace_id: str,
    video_id: str,
    transcript_version_id: str,
    source: str,
    language_code: str | None,
    content_hash: str,
    metadata: Mapping[str, Any] | None = None,
) -> SqlStatement:
    statement = insert(transcript_versions).values(
        id=transcript_version_id,
        workspace_id=workspace_id,
        video_id=video_id,
        source=source,
        language_code=language_code,
        content_hash=content_hash,
        metadata_json=_json_param(metadata or {}),
    )
    statement = statement.on_conflict_do_update(
        index_elements=[transcript_versions.c.id],
        set_={
            "source": statement.excluded.source,
            "language_code": statement.excluded.language_code,
            "content_hash": statement.excluded.content_hash,
            "metadata_json": statement.excluded.metadata_json,
        },
    ).returning(transcript_versions)
    return _sql_statement(statement)


def upsert_chunk_sql(
    *,
    workspace_id: str,
    hosted_video_id: str,
    transcript_version_id: str,
    index_profile_id: str,
    tokenizer: str,
    chunk: TranscriptChunkInput,
    chunk_id: str,
) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO chunks (
    id, workspace_id, video_id, transcript_version_id, index_profile_id,
    chunk_index, start_seconds, end_seconds, text, bm25_document, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(video_id)s, %(transcript_version_id)s,
    %(index_profile_id)s, %(chunk_index)s, %(start_seconds)s, %(end_seconds)s,
    %(text)s, tokenize(%(text)s, %(tokenizer)s)::bm25vector, %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, transcript_version_id, index_profile_id, chunk_index) DO UPDATE
SET start_seconds = EXCLUDED.start_seconds,
    end_seconds = EXCLUDED.end_seconds,
    text = EXCLUDED.text,
    bm25_document = EXCLUDED.bm25_document,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": chunk_id,
            "workspace_id": workspace_id,
            "video_id": hosted_video_id,
            "transcript_version_id": transcript_version_id,
            "index_profile_id": index_profile_id,
            "chunk_index": chunk.chunk_index,
            "start_seconds": chunk.start_seconds,
            "end_seconds": chunk.end_seconds,
            "text": chunk.text,
            "tokenizer": tokenizer,
            "metadata_json": _json_param(chunk.metadata or {}),
        },
    )


def upsert_chunk_embedding_sql(
    *,
    workspace_id: str,
    chunk_id: str,
    index_profile_id: str,
    embedding: Sequence[float],
    embedding_id: str,
    usage_reservation_id: str,
) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO chunk_embeddings (
    id, workspace_id, chunk_id, index_profile_id, embedding, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(chunk_id)s, %(index_profile_id)s,
    %(embedding)s::vector, %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, chunk_id, index_profile_id) DO UPDATE
SET embedding = EXCLUDED.embedding,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": embedding_id,
            "workspace_id": workspace_id,
            "chunk_id": chunk_id,
            "index_profile_id": index_profile_id,
            "embedding": _vector_literal(embedding),
            "metadata_json": _json_param({"usage_reservation_id": usage_reservation_id}),
        },
    )


def upsert_job_operation_sql(operation: JobOperation) -> SqlStatement:
    statement = insert(job_operations).values(
        id=operation.id,
        workspace_id=operation.workspace_id,
        job_id=operation.job_id,
        operation=operation.operation,
        source_id=operation.source_id,
        video_id=operation.video_id,
        input_hash=operation.input_hash,
        idempotency_key=operation.idempotency_key,
        status=operation.status,
        attempt_count=operation.attempt_count,
        usage_reservation_id=operation.metadata_jsonb.get("usage_reservation_id") or None,
        metadata_json=_json_param(operation.metadata_jsonb),
    )
    terminal_statuses = ("denied", "succeeded", "failed_final", "reconciled", "released")
    early_statuses = ("planned", "reserved", "started")
    statement = statement.on_conflict_do_update(
        index_elements=[job_operations.c.workspace_id, job_operations.c.idempotency_key],
        set_={
            "status": case(
                (
                    job_operations.c.status.in_(terminal_statuses)
                    & statement.excluded.status.in_(early_statuses),
                    job_operations.c.status,
                ),
                else_=statement.excluded.status,
            ),
            "attempt_count": job_operations.c.attempt_count,
            "usage_reservation_id": statement.excluded.usage_reservation_id,
            "metadata_json": statement.excluded.metadata_json,
            "updated_at": func.now(),
        },
    ).returning(job_operations)
    return _sql_statement(statement)


def job_operation_output_sql(*, workspace_id: str, operation_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT status, output_json
FROM job_operations
WHERE workspace_id = %(workspace_id)s
  AND id = %(operation_id)s;
""".strip(),
        params={"workspace_id": workspace_id, "operation_id": operation_id},
    )


def update_job_operation_output_sql(
    *,
    workspace_id: str,
    operation_id: str,
    output: Mapping[str, Any],
    now: Any,
    usage_reservation_id: str | None = None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE job_operations
SET output_json = %(output_json)s::jsonb,
    usage_reservation_id = COALESCE(%(usage_reservation_id)s::text, usage_reservation_id),
    updated_at = %(now)s
WHERE workspace_id = %(workspace_id)s
  AND id = %(operation_id)s
RETURNING *;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "operation_id": operation_id,
            "output_json": _json_param(output),
            "usage_reservation_id": usage_reservation_id,
            "now": now,
        },
    )


def complete_job_operation_success_sql(
    *,
    workspace_id: str,
    operation_id: str,
    output: Mapping[str, Any],
    now: Any,
    usage_reservation_id: str | None = None,
    job_id: str | None = None,
    lease_owner: str | None = None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
UPDATE job_operations
SET output_json = %(output_json)s::jsonb,
    status = 'succeeded',
    usage_reservation_id = COALESCE(%(usage_reservation_id)s::text, usage_reservation_id),
    updated_at = %(now)s
WHERE workspace_id = %(workspace_id)s
  AND id = %(operation_id)s
  AND (
      %(job_id)s::text IS NULL
      OR EXISTS (
          SELECT 1
          FROM jobs
          WHERE jobs.id = %(job_id)s::text
            AND jobs.workspace_id = job_operations.workspace_id
            AND jobs.lease_owner = %(lease_owner)s::text
            AND jobs.lease_expires_at > %(now)s
            AND jobs.status <> ALL(%(terminal_statuses)s::text[])
      )
  )
RETURNING *;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "operation_id": operation_id,
            "output_json": _json_param(output),
            "usage_reservation_id": usage_reservation_id,
            "now": now,
            "job_id": job_id,
            "lease_owner": lease_owner,
            "terminal_statuses": ["cancelled", "denied", "failed", "succeeded"],
        },
    )


def provider_success_usage_event_sql(*, workspace_id: str, idempotency_key: str) -> SqlStatement:
    return SqlStatement(
        sql="""
SELECT id
FROM usage_events
WHERE workspace_id = %(workspace_id)s
  AND event_type = 'provider_attempt_succeeded'
  AND status = 'succeeded'
  AND (
      metadata_json->>'idempotency_key' = %(idempotency_key)s
      OR metadata_json->>'parent_idempotency_key' = %(idempotency_key)s
  )
ORDER BY created_at DESC, id DESC
LIMIT 1;
""".strip(),
        params={"workspace_id": workspace_id, "idempotency_key": idempotency_key},
    )


def enqueue_index_video_job_sql(
    *,
    workspace_id: str,
    source_id: str,
    video_id: str,
    priority: int,
    now: Any,
    metadata: Mapping[str, Any] | None = None,
) -> SqlStatement:
    idempotency_payload = {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "video_id": video_id,
        "job_type": "index_video",
        "profile": _real_index_profile_extras(workspace_id),
    }
    job_hash = input_hash(idempotency_payload, prefix="").lstrip("_")[:24]
    idempotency = job_operation_idempotency_key(
        workspace_id=workspace_id,
        operation="jobs.index_video",
        input_hash_value=input_hash(idempotency_payload),
        source_id=source_id,
        video_id=video_id,
        extras=_real_index_profile_extras(workspace_id),
    )
    statement = insert(jobs).values(
        id=f"job_{job_hash}",
        workspace_id=workspace_id,
        source_id=source_id,
        job_type="index_video",
        status="queued",
        priority=priority,
        idempotency_key=idempotency,
        run_after=now,
        executor_kind="railway",
        executor_ref="source_discovery",
        metadata_json=_json_param({"youtube_video_id": video_id, **dict(metadata or {})}),
        created_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[jobs.c.workspace_id, jobs.c.idempotency_key],
        set_={
            "source_id": statement.excluded.source_id,
            "status": case(
                (
                    jobs.c.status.in_(("denied", "failed", "succeeded", "cancelled")),
                    jobs.c.status,
                ),
                else_=literal_column("'queued'"),
            ),
            "priority": func.least(jobs.c.priority, statement.excluded.priority),
            "metadata_json": jobs.c.metadata_json.op("||")(statement.excluded.metadata_json),
        },
    ).returning(jobs)
    return _sql_statement(statement)


def enqueue_discover_source_job_sql(
    *,
    workspace_id: str,
    source_id: str,
    priority: int,
    now: Any,
    policy_id: str | None = None,
    max_new_videos_per_run: int | None = None,
    trigger: str = "manual_source_import",
    metadata: Mapping[str, Any] | None = None,
) -> SqlStatement:
    request_bucket = _job_request_bucket(now)
    idempotency_payload = {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "job_type": "discover_source",
        "trigger": trigger,
        "request_bucket": request_bucket,
    }
    job_hash = input_hash(idempotency_payload, prefix="").lstrip("_")[:24]
    idempotency = job_operation_idempotency_key(
        workspace_id=workspace_id,
        operation="jobs.discover_source",
        input_hash_value=input_hash(idempotency_payload),
        source_id=source_id,
        extras=(trigger, request_bucket),
    )
    metadata_payload = {
        "source_refresh_policy_id": policy_id,
        "max_new_videos_per_run": max_new_videos_per_run,
        "trigger": trigger,
        **dict(metadata or {}),
    }
    statement = insert(jobs).values(
        id=f"job_{job_hash}",
        workspace_id=workspace_id,
        source_id=source_id,
        job_type="discover_source",
        status="queued",
        priority=priority,
        idempotency_key=idempotency,
        run_after=now,
        executor_kind="railway",
        executor_ref="source_discovery",
        metadata_json=_json_param(metadata_payload),
        created_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[jobs.c.workspace_id, jobs.c.idempotency_key],
        set_={
            "source_id": statement.excluded.source_id,
            "status": case(
                (
                    jobs.c.status.in_(("denied", "failed", "succeeded", "cancelled")),
                    jobs.c.status,
                ),
                else_=literal_column("'queued'"),
            ),
            "priority": func.least(jobs.c.priority, statement.excluded.priority),
            "metadata_json": jobs.c.metadata_json.op("||")(statement.excluded.metadata_json),
        },
    ).returning(jobs)
    return _sql_statement(statement)


def finish_source_discovery_sql(
    *,
    workspace_id: str,
    source_id: str,
    policy_id: str | None,
    now: Any,
    discovered_videos: int,
    enqueued_jobs: int,
    video_ids: Sequence[str],
    lease_owner: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> SqlStatement:
    return SqlStatement(
        sql="""
WITH source_update AS (
    UPDATE sources
    SET last_discovered_at = %(now)s,
        status = CASE WHEN %(error_code)s::text IS NULL THEN 'active' ELSE status END,
        metadata_json = metadata_json || %(source_metadata_json)s::jsonb,
        updated_at = %(now)s
    WHERE workspace_id = %(workspace_id)s
      AND id = %(source_id)s
    RETURNING id
),
policy_update AS (
    UPDATE source_refresh_policies
    SET last_succeeded_at = CASE WHEN %(error_code)s::text IS NULL THEN %(now)s ELSE last_succeeded_at END,
        failure_code = %(error_code)s,
        failure_message = %(error_message)s,
        cursor_json = cursor_json || %(cursor_json)s::jsonb,
        locked_by = CASE WHEN locked_by = %(lease_owner)s THEN NULL ELSE locked_by END,
        locked_until = CASE WHEN locked_by = %(lease_owner)s THEN NULL ELSE locked_until END,
        updated_at = %(now)s
    WHERE workspace_id = %(workspace_id)s
      AND (%(policy_id)s::text IS NOT NULL AND id = %(policy_id)s)
    RETURNING id
)
SELECT
    (SELECT count(*) FROM source_update) AS sources_updated,
    (SELECT count(*) FROM policy_update) AS policies_updated;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "source_id": source_id,
            "policy_id": policy_id,
            "now": now,
            "lease_owner": lease_owner,
            "error_code": error_code,
            "error_message": error_message,
            "source_metadata_json": _json_param(
                {
                    "last_discovery": {
                        "discovered_videos": discovered_videos,
                        "enqueued_jobs": enqueued_jobs,
                        "video_ids": list(video_ids),
                        "error_code": error_code,
                    }
                }
            ),
            "cursor_json": _json_param(
                {
                    "last_discovered_at": getattr(now, "isoformat", lambda: str(now))(),
                    "last_video_ids": list(video_ids),
                }
            ),
        },
    )


def mock_embedding_vector(text: str, *, dimension: int = DEFAULT_EMBEDDING_DIMENSION, salt: str = "") -> list[float]:
    if dimension <= 0:
        raise ValueError("dimension must be positive")
    digest = input_hash({"text": text, "salt": salt}, prefix="").lstrip("_")
    values: list[float] = []
    cursor = 0
    while len(values) < dimension:
        if cursor + 8 > len(digest):
            digest += input_hash({"digest": digest}, prefix="").lstrip("_")
            cursor = 0
        bucket = int(digest[cursor : cursor + 8], 16)
        values.append(round((bucket / 0xFFFFFFFF) * 2.0 - 1.0, 6))
        cursor += 8
    return values


def _validate_public_indexing_inputs(
    *,
    source: Source,
    job: Job,
    video: HostedVideoInput,
    chunks: Sequence[TranscriptChunkInput],
) -> None:
    decision = source_discovery_decision(source)
    if not source.is_public_source or not decision.discoverable:
        raise ValueError(f"source is not public and discoverable: {decision.code}")
    if source.workspace_id != job.workspace_id:
        raise ValueError("source and job must belong to the same workspace")
    if job.source_id is not None and job.source_id != source.id:
        raise ValueError("job source_id does not match source")
    if job.job_type != "index_video":
        raise ValueError("job_type must be index_video")
    if source.canonical_video_id is not None and source.canonical_video_id != video.youtube_video_id:
        raise ValueError("source canonical video does not match video")
    if not YOUTUBE_VIDEO_ID_RE.fullmatch(video.youtube_video_id):
        raise ValueError("video.youtube_video_id must be an 11 character YouTube id")
    if not chunks:
        raise ValueError("at least one transcript chunk is required")
    indexes = [chunk.chunk_index for chunk in chunks]
    if len(set(indexes)) != len(indexes):
        raise ValueError("chunk indexes must be unique")
    for chunk in chunks:
        if chunk.chunk_index < 0:
            raise ValueError("chunk indexes must be non-negative")
        if not chunk.text.strip():
            raise ValueError("chunk text is required")


def _reserve_usage(
    *,
    workspace_id: str,
    subject: UsageSubject,
    operation: str,
    estimated_units: dict[str, float],
    allocations: Sequence[Allocation],
    policy: EntitlementPolicy,
    balance: WorkspaceBalance,
    gate: UsageGate,
    subject_id: str,
    input_payload: Mapping[str, Any],
    extras: Sequence[str],
    created_at: Any,
) -> UsageReservation:
    operation_input_hash = input_hash({"subject": subject, "operation": operation, "input": input_payload})
    reservation_key = job_operation_idempotency_key(
        workspace_id=workspace_id,
        operation=f"{subject}.{operation}",
        input_hash_value=operation_input_hash,
        video_id=subject_id,
        extras=extras,
    )
    resolution = resolve_allocation(allocations, workspace_id=workspace_id, subject=subject, operation=operation)
    reservation = gate.reserve(
        workspace_id=workspace_id,
        subject=subject,
        operation=operation,
        estimated_units=estimated_units,
        allocation=resolution.allocation,
        policy=policy,
        balance=balance,
        idempotency_key=reservation_key,
    )
    return reservation.model_copy(
        update={
                "id": stable_usage_reservation_id(workspace_id=workspace_id, idempotency_key=reservation_key),
            "created_at": created_at,
            "metadata": {
                **reservation.metadata,
                "allocation_resolution": resolution.reason,
                "input_hash": operation_input_hash,
                "idempotency_extras": list(extras),
            },
        }
    )


def _job_operation(
    *,
    workspace_id: str,
    job_id: str,
    source_id: str,
    video_id: str,
    operation: str,
    input_payload: Mapping[str, Any],
    idempotency_extras: Sequence[str],
    usage_reservation_id: str,
    created_at: Any,
) -> JobOperation:
    operation_input_hash = input_hash({"operation": operation, "input": input_payload})
    key = job_operation_idempotency_key(
        workspace_id=workspace_id,
        operation=operation,
        input_hash_value=operation_input_hash,
        source_id=source_id,
        video_id=video_id,
        extras=idempotency_extras,
    )
    return JobOperation(
        id=_stable_id("op", workspace_id, job_id, operation, operation_input_hash, *idempotency_extras),
        workspace_id=workspace_id,
        job_id=job_id,
        operation=operation,
        source_id=source_id,
        video_id=video_id,
        input_hash=operation_input_hash,
        idempotency_key=key,
        status="reserved",
        created_at=created_at,
        updated_at=created_at,
        metadata_jsonb={
            "usage_reservation_id": usage_reservation_id,
            "idempotency_extras": list(idempotency_extras),
        },
    )


def _default_allocations(workspace_id: str, index_profile_id: str) -> tuple[Allocation, ...]:
    return (
        ProviderAllocation(
            id=f"alloc_{workspace_id}_voyage_mock",
            workspace_id=workspace_id,
            provider="voyage",
            operation="embed_documents",
            credential_mode="hosted",
            model_or_plan=DEFAULT_EMBEDDING_MODEL,
        ),
        default_search_store_allocation(workspace_id=workspace_id, operation="*", index_profile_ref=index_profile_id),
    )


def _default_search_query(chunks: Sequence[TranscriptChunkInput]) -> str:
    first = chunks[0].text.strip().split()
    return " ".join(first[: min(3, len(first))]) or "transcript"


def _duration_seconds_from_job(job: Job) -> int | None:
    value = job.metadata_jsonb.get("duration_seconds")
    if value is None:
        return None
    try:
        duration = int(float(value))
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _chunk_hash_payload(chunk: TranscriptChunkInput) -> dict[str, Any]:
    return {
        "chunk_index": chunk.chunk_index,
        "start_seconds": chunk.start_seconds,
        "end_seconds": chunk.end_seconds,
        "text": chunk.text,
        "metadata": dict(chunk.metadata or {}),
    }


def _index_profile_identity(profile: IndexProfileInput) -> dict[str, Any]:
    return {
        "id": profile.id,
        "backend": profile.backend,
        "embedding_model": profile.embedding_model,
        "embedding_dimension": profile.embedding_dimension,
        "chunking_version": profile.chunking_version,
        "tokenizer": profile.tokenizer,
    }


def _index_profile_fingerprint(profile: IndexProfileInput) -> str:
    return input_hash(_index_profile_identity(profile), prefix="sip")


def _default_mock_index_profile(workspace_id: str) -> IndexProfileInput:
    profile = IndexProfileInput()
    return replace(profile, id=_workspace_index_profile_id(workspace_id, profile))


def _default_real_index_profile(workspace_id: str) -> IndexProfileInput:
    profile = IndexProfileInput(chunking_version=REAL_HOSTED_CHUNKING_VERSION)
    return replace(profile, id=_workspace_index_profile_id(workspace_id, profile))


def _real_index_profile_extras(workspace_id: str) -> tuple[str, str]:
    profile = _default_real_index_profile(workspace_id)
    return (profile.id, _index_profile_fingerprint(profile))


def _job_request_bucket(now: Any) -> str:
    try:
        bucketed = now.replace(second=0, microsecond=0)
    except (AttributeError, TypeError, ValueError):
        return str(now)
    if hasattr(bucketed, "isoformat"):
        return bucketed.isoformat()
    return str(bucketed)


def _workspace_index_profile_id(workspace_id: str, profile: IndexProfileInput) -> str:
    return _stable_id(
        "sip",
        workspace_id,
        profile.backend,
        profile.embedding_model,
        str(profile.embedding_dimension),
        profile.chunking_version,
        profile.tokenizer,
    )


def _token_estimate(text: str) -> int:
    return max(1, len(text.split()))


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{input_hash(list(parts), prefix='').lstrip('_')[:24]}"


def _chunks_from_normalized_transcript(transcript: NormalizedTranscript) -> list[TranscriptChunkInput]:
    return [
        TranscriptChunkInput(
            chunk_index=chunk.sequence,
            start_seconds=chunk.start_ms / 1000,
            end_seconds=chunk.end_ms / 1000,
            text=chunk.text,
            metadata={
                "token_count": chunk.token_count,
                "text_hash": chunk.text_hash,
                "segment_ids": chunk.segment_ids,
                "forced_split": chunk.forced_split,
                "chunker_version": REAL_HOSTED_CHUNKING_VERSION,
            },
        )
        for chunk in build_chunks(
            video_id=transcript.video_id,
            transcript_version_id=transcript.version_id,
            segments=transcript.segments,
        )
    ]


def _transcript_to_output(transcript: NormalizedTranscript) -> dict[str, Any]:
    return {
        "version_id": transcript.version_id,
        "video_id": transcript.video_id,
        "source": transcript.source,
        "language": transcript.language,
        "is_generated": transcript.is_generated,
        "text_hash": transcript.text_hash,
        "segments": [
            {
                "segment_id": segment.segment_id,
                "sequence": segment.sequence,
                "start_ms": segment.start_ms,
                "end_ms": segment.end_ms,
                "text": segment.text,
            }
            for segment in transcript.segments
        ],
    }


def _transcript_from_output(output: Mapping[str, Any]) -> NormalizedTranscript:
    return NormalizedTranscript(
        version_id=str(output["version_id"]),
        video_id=str(output["video_id"]),
        source=str(output["source"]),
        language=_optional_str(output.get("language")),
        is_generated=bool(output.get("is_generated")),
        text_hash=str(output["text_hash"]),
        segments=[
            TranscriptSegment(
                segment_id=str(segment["segment_id"]),
                sequence=int(segment["sequence"]),
                start_ms=int(segment["start_ms"]),
                end_ms=int(segment["end_ms"]),
                text=str(segment["text"]),
            )
            for segment in output.get("segments", [])
        ],
    )


def _vectors_from_output(output: Any) -> list[list[float]]:
    if not isinstance(output, list):
        raise HostedIndexingError("cached voyage output is not a vector list")
    return [[float(value) for value in vector] for vector in output]


def _source_from_row(row: Mapping[str, Any]) -> Source:
    return Source(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        source_type=row["source_type"],
        source_url=str(row["source_url"]),
        canonical_channel_id=_optional_str(row.get("canonical_channel_id")),
        canonical_playlist_id=_optional_str(row.get("canonical_playlist_id")),
        canonical_video_id=_optional_str(row.get("canonical_video_id")),
        display_name=_optional_str(row.get("display_name")),
        selected=bool(row.get("selected", True)),
        auto_index_allowed=bool(row.get("auto_index_allowed", True)),
        import_source=row["import_source"],
        auth_grant_id=_optional_str(row.get("auth_grant_id")),
        metadata_jsonb=dict(_json_value(row.get("metadata_json"))),
        status=row.get("status", "active"),
        last_discovered_at=row.get("last_discovered_at"),
        last_indexed_at=row.get("last_indexed_at"),
    )


def _execute_one(connection: Any, statement: SqlStatement) -> Mapping[str, Any] | None:
    rows = _execute_rows(connection, statement)
    return rows[0] if rows else None


def _execute_rows(connection: Any, statement: SqlStatement) -> list[dict[str, Any]]:
    return _rows_from_result(connection.execute(statement.sql, statement.params))


def _rows_from_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    if isinstance(result, list):
        return [dict(row) for row in result]
    if hasattr(result, "mappings"):
        return [dict(row) for row in result.mappings()]
    if hasattr(result, "fetchall"):
        return [dict(row) for row in result.fetchall()]
    try:
        return [dict(row) for row in result]
    except TypeError:
        return []


def _json_value(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, bytes):
        return json.loads(value.decode("utf-8"))
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in vector) + "]"


def _sql_statement(statement: Any) -> SqlStatement:
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)


def _json_param(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _is_youtube_host(host: str) -> bool:
    normalized = host.lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    return normalized in {"youtube.com", "m.youtube.com", "music.youtube.com"} or normalized.endswith(".youtube.com")


def _extract_handle(value: str) -> str | None:
    parsed = urlsplit(value if "://" in value else f"https://www.youtube.com/{value.lstrip('/')}")
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].startswith("@") and len(parts[0]) > 1:
        return parts[0][1:]
    bare = value.strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", bare) and not YOUTUBE_VIDEO_ID_RE.fullmatch(bare):
        return bare
    return None


def _extract_playlist_id(value: str) -> str | None:
    parsed = urlsplit(value if "://" in value else f"https://www.youtube.com/{value.lstrip('/')}")
    query_id = parse_qs(parsed.query).get("list", [None])[0]
    if query_id:
        return str(query_id)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "playlist":
        return parts[1]
    return None


def _extract_channel_id(value: str) -> str | None:
    parsed = urlsplit(value if "://" in value else f"https://www.youtube.com/{value.lstrip('/')}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "channel" and parts[1].startswith("UC"):
        return parts[1]
    stripped = value.strip()
    if stripped.startswith("UC") and re.fullmatch(r"UC[A-Za-z0-9_-]{20,}", stripped):
        return stripped
    return None


def _looks_retryable_discovery_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in ("timeout", "temporarily", "try again", "connection reset", "unavailable"))


def _looks_retryable_indexing_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "too many requests",
            "timeout",
            "temporarily",
            "try again",
            "connection reset",
            "connection aborted",
            "unavailable",
            "server error",
            "service unavailable",
        )
    )


__all__ = [
    "DEFAULT_CHUNKING_VERSION",
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_INDEX_PROFILE_ID",
    "HostedIndexingPlan",
    "HostedIndexingExecutionResult",
    "HostedIndexingExecutor",
    "HostedIndexingError",
    "HostedSourceDiscoveryExecutionResult",
    "HostedSourceDiscoveryExecutor",
    "HostedVideoInput",
    "HostedWebshareRequired",
    "IndexProfileInput",
    "PlannedSqlOperation",
    "REAL_HOSTED_CHUNKING_VERSION",
    "TranscriptChunkInput",
    "extract_public_youtube_video_id",
    "enqueue_discover_source_job_sql",
    "enqueue_index_video_job_sql",
    "finish_source_discovery_sql",
    "complete_job_operation_success_sql",
    "job_operation_output_sql",
    "mock_embedding_vector",
    "plan_mock_hosted_public_indexing",
    "plan_real_hosted_public_indexing",
    "source_from_public_youtube_input",
    "upsert_chunk_embedding_sql",
    "upsert_chunk_sql",
    "upsert_index_profile_sql",
    "upsert_job_operation_sql",
    "upsert_video_sql",
    "update_job_operation_output_sql",
    "provider_success_usage_event_sql",
]
