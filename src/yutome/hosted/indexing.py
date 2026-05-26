from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

from yutome.chunking import CHUNKER_VERSION, build_chunks
from yutome.config import AppConfig
from yutome.gemini import transcribe_youtube_url_with_gemini
from yutome.hosted.allocations import Allocation, resolve_allocation
from yutome.hosted.allocation_policy import default_search_store_allocation
from yutome.hosted.control_plane import (
    Job,
    JobOperation,
    Source,
    job_operation_idempotency_key,
    source_discovery_decision,
)
from yutome.hosted.gate import UsageGate
from yutome.hosted.ids import input_hash
from yutome.hosted.ledger import PostgresUsageGate, PostgresUsageLedger
from yutome.hosted.migrations import (
    HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    HOSTED_DEFAULT_EMBEDDING_MODEL,
    HOSTED_VECTOR_BACKEND,
)
from yutome.hosted.models import EntitlementPolicy, ProviderAllocation, UsageNormalization, UsageReservation, UsageSubject, WorkspaceBalance
from yutome.hosted.provider_wrappers import ProviderCallContext, UsageReservationDenied, execute_provider_call
from yutome.hosted.repositories import SqlStatement, upsert_usage_reservation_sql
from yutome.hosted.search_store import (
    SearchStoreQueryPlan,
    hybrid_query_plan,
    replace_active_transcript_sql,
    validate_supported_embedding_profile,
)
from yutome.quality_llm import TranscriptCleanupContext, cleanup_transcript_with_gemini
from yutome.transcripts import NormalizedTranscript, normalize_transcript
from yutome.youtube import (
    TranscriptFetchResult,
    discover_video,
    fetch_subtitle_transcript_with_ytdlp,
    fetch_transcript,
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
    published_at: str | None = None
    metadata: Mapping[str, Any] | None = None


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
    tokenizer: str = "pg_tokenizer"
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
    status: Literal["succeeded", "denied", "failed", "cancelled"]
    hosted_video_id: str | None = None
    transcript_version_id: str | None = None
    chunks_written: int = 0
    embeddings_written: int = 0
    denied_operation: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class HostedIndexingError(RuntimeError):
    code = "hosted_indexing_failed"


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
    profile = index_profile or IndexProfileInput()
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
) -> HostedIndexingPlan:
    """Build replay-safe hosted write operations for a real transcript and embeddings."""

    _validate_public_indexing_inputs(source=source, job=job, video=video, chunks=chunks)
    profile = index_profile or IndexProfileInput(chunking_version=REAL_HOSTED_CHUNKING_VERSION)
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
                    usage_reservation_id="",
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
        self.metadata_fetcher = metadata_fetcher or self._fetch_video_metadata
        self.transcript_fetcher = transcript_fetcher or self._fetch_transcript
        self.gemini_cleaner = gemini_cleaner or self._clean_with_gemini
        self.voyage_embedder = voyage_embedder or self._embed_with_voyage
        self.cwd = cwd or Path.cwd()

    def execute(self, job: Job, *, lease_owner: str, now: Any | None = None) -> HostedIndexingExecutionResult:
        from datetime import datetime, timezone
        from yutome.hosted.jobs import update_job_operation_status_sql, update_job_status_sql

        clock = now or datetime.now(timezone.utc)
        source_id = job.source_id or ""
        video_id: str | None = None
        current_operation_id: str | None = None
        current_operation_name: str | None = None
        try:
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="preparing", now=clock))
            source = self._load_source(job)
            video_id = source.canonical_video_id or extract_public_youtube_video_id(source.source_url)
            if not video_id:
                raise HostedIndexingError("index_video jobs require a concrete public YouTube video id")
            video = self.metadata_fetcher(video_id, source, job)
            transcript_result = self.transcript_fetcher(video.youtube_video_id, source, job)
            transcript = normalize_transcript(
                video_id=video.youtube_video_id,
                raw_snippets=transcript_result.raw_snippets,
                source=transcript_result.source,
                language=transcript_result.language,
                is_generated=transcript_result.is_generated,
            )
            if not transcript.segments:
                raise HostedIndexingError("transcript had no usable text segments")

            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="cleaning", now=clock))
            gemini_context = self._provider_context(
                workspace_id=job.workspace_id,
                subject="gemini",
                operation="cleanup_transcript",
                estimated_units={"total_tokens": float(_token_estimate(" ".join(segment.text for segment in transcript.segments)))},
                subject_id=video.youtube_video_id,
                input_payload={"transcript_version_id": transcript.version_id, "source": transcript.source},
                metadata={"job_id": job.id, "source_id": source.id, "video_id": video.youtube_video_id},
            )
            current_operation_name = "gemini.cleanup_transcript"
            current_operation_id = self._upsert_operation(job, source, video.youtube_video_id, current_operation_name, gemini_context)
            transcript = self.gemini_cleaner(transcript, video, gemini_context)
            self._execute_statement(
                update_job_operation_status_sql(
                    operation_id=current_operation_id,
                    workspace_id=job.workspace_id,
                    status="succeeded",
                    now=clock,
                )
            )
            current_operation_id = None
            current_operation_name = None

            chunks = _chunks_from_normalized_transcript(transcript)
            if not chunks:
                raise HostedIndexingError("chunking produced no indexable chunks")
            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="embedding", now=clock))
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
            )
            current_operation_name = "voyage.embed_documents"
            current_operation_id = self._upsert_operation(job, source, video.youtube_video_id, current_operation_name, voyage_context)
            vectors = self.voyage_embedder(chunks, video, voyage_context)
            self._execute_statement(
                update_job_operation_status_sql(
                    operation_id=current_operation_id,
                    workspace_id=job.workspace_id,
                    status="succeeded",
                    now=clock,
                )
            )
            current_operation_id = None
            current_operation_name = None

            self._execute_statement(update_job_status_sql(job_id=job.id, lease_owner=lease_owner, status="writing_index", now=clock))
            current_operation_name = "search_store.index_write"
            current_operation_id = self._upsert_raw_operation(
                job,
                source,
                video.youtube_video_id,
                current_operation_name,
                {"chunks": [_chunk_hash_payload(chunk) for chunk in chunks], "vectors": len(vectors)},
            )
            search_reservation = self._reserve_search_write(job=job, source=source, video=video, chunks=chunks, vectors=vectors)
            if not search_reservation.decision.allowed:
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
                },
            )
            with self._transaction():
                for operation in plan.sql_operations:
                    self._execute_statement(operation.statement)
            self._execute_statement(
                update_job_operation_status_sql(
                    operation_id=current_operation_id,
                    workspace_id=job.workspace_id,
                    status="succeeded",
                    now=clock,
                )
            )
            current_operation_id = None
            current_operation_name = None
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
                denied_operation=current_operation_name,
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
            if current_operation_id is not None:
                self._execute_statement(
                    update_job_operation_status_sql(
                        operation_id=current_operation_id,
                        workspace_id=job.workspace_id,
                        status="failed_final",
                        now=clock,
                        error_code=type(exc).__name__,
                        error_message=str(exc),
                    )
                )
            self._execute_statement(
                update_job_status_sql(
                    job_id=job.id,
                    lease_owner=lease_owner,
                    status="failed",
                    now=clock,
                    error_code=getattr(exc, "code", type(exc).__name__),
                    error_message=str(exc),
                )
            )
            return HostedIndexingExecutionResult(
                job_id=job.id,
                workspace_id=job.workspace_id,
                source_id=source_id,
                youtube_video_id=video_id,
                status="failed",
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=str(exc),
            )

    def _fetch_video_metadata(self, video_id: str, source: Source, job: Job) -> HostedVideoInput:
        discovered = discover_video(
            target=video_id,
            cwd=self.cwd,
            proxy=self.config.proxy if self.config.proxy.use_for_metadata else None,
            ytdlp_config=self.config.yt_dlp,
        )
        return HostedVideoInput(
            youtube_video_id=discovered.video_id,
            title=discovered.title or source.display_name or discovered.video_id,
            url=discovered.url,
            channel_id=discovered.channel_id,
            duration_seconds=discovered.duration_seconds,
            metadata={
                "channel_title": discovered.channel_title,
                "channel_handle": discovered.channel_handle,
                "playlist_tab": discovered.playlist_tab,
                "source": "yt_dlp",
            },
        )

    def _fetch_transcript(self, video_id: str, source: Source, job: Job) -> TranscriptFetchResult:
        proxy = self.config.proxy if self.config.proxy.enabled else None
        try:
            if self.config.transcripts.prefer_ytdlp_subtitles:
                return fetch_subtitle_transcript_with_ytdlp(
                    video_id=video_id,
                    cwd=self.cwd,
                    language=self.config.transcripts.preferred_languages[0],
                    proxy=proxy,
                    ytdlp_config=self.config.yt_dlp,
                    allow_translated_captions=self.config.transcripts.allow_translated_captions,
                )
            return fetch_transcript(
                video_id=video_id,
                languages=self.config.transcripts.preferred_languages,
                proxy=proxy,
                timeout_seconds=self.config.transcripts.request_timeout_seconds,
            )
        except Exception:
            if not self.config.gemini.fallback_enabled:
                raise
            return transcribe_youtube_url_with_gemini(video_id=video_id, config=self.config.gemini)

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
        estimated_units: Mapping[str, float],
        subject_id: str,
        input_payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> ProviderCallContext:
        allocations = self._load_allocations(workspace_id)
        resolution = resolve_allocation(allocations, workspace_id=workspace_id, subject=subject, operation=operation)
        operation_input_hash = input_hash({"subject": subject, "operation": operation, "input": input_payload})
        reservation_key = job_operation_idempotency_key(
            workspace_id=workspace_id,
            operation=f"{subject}.{operation}",
            input_hash_value=operation_input_hash,
            video_id=subject_id,
            extras=[DEFAULT_INDEX_PROFILE_ID],
        )
        return ProviderCallContext(
            gate=self.gate,
            ledger=self.ledger,
            workspace_id=workspace_id,
            subject=subject,
            operation=operation,
            estimated_units=dict(estimated_units),
            allocation=resolution.allocation,
            policy=self._load_policy(workspace_id),
            balance=self._load_balance(workspace_id),
            idempotency_key=reservation_key,
            metadata={**dict(metadata), "input_hash": operation_input_hash, "allocation_resolution": resolution.reason},
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
        reservation = _reserve_usage(
            workspace_id=job.workspace_id,
            subject="search_store",
            operation="index_write",
            estimated_units={
                "transcript_versions": 1.0,
                "chunks": float(len(chunks)),
                "embeddings": float(len(vectors)),
            },
            allocations=self._load_allocations(job.workspace_id),
            policy=self._load_policy(job.workspace_id),
            balance=self._load_balance(job.workspace_id),
            gate=self.gate,
            subject_id=video.youtube_video_id,
            input_payload={"job_id": job.id, "chunks": [_chunk_hash_payload(chunk) for chunk in chunks]},
            extras=[DEFAULT_INDEX_PROFILE_ID],
            created_at=job.created_at,
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
            idempotency_extras=[DEFAULT_INDEX_PROFILE_ID],
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
    ) -> str:
        operation = _job_operation(
            workspace_id=job.workspace_id,
            job_id=job.id,
            source_id=source.id,
            video_id=video_id,
            operation=operation_name,
            input_payload=input_payload,
            idempotency_extras=[DEFAULT_INDEX_PROFILE_ID],
            usage_reservation_id="",
            created_at=job.created_at,
        )
        self._execute_statement(upsert_job_operation_sql(operation))
        return operation.id

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
                    mode=row["mode"],
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
                    mode=row.get("mode", "service_internal"),
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
            max_units_by_operation=dict(_json_value(row.get("hard_limits_jsonb"))),
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


def upsert_video_sql(source: Source, video: HostedVideoInput, *, hosted_video_id: str) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO videos (
    id, workspace_id, source_id, youtube_video_id, channel_id, title,
    description, published_at, duration_seconds, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(source_id)s, %(youtube_video_id)s, %(channel_id)s,
    %(title)s, %(description)s, %(published_at)s, %(duration_seconds)s,
    %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, youtube_video_id) DO UPDATE
SET source_id = EXCLUDED.source_id,
    channel_id = EXCLUDED.channel_id,
    title = EXCLUDED.title,
    description = EXCLUDED.description,
    published_at = EXCLUDED.published_at,
    duration_seconds = EXCLUDED.duration_seconds,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": hosted_video_id,
            "workspace_id": source.workspace_id,
            "source_id": source.id,
            "youtube_video_id": video.youtube_video_id,
            "channel_id": video.channel_id,
            "title": video.title,
            "description": video.description,
            "published_at": video.published_at,
            "duration_seconds": video.duration_seconds,
            "metadata_json": _json_param(video.metadata or {}),
        },
    )


def upsert_index_profile_sql(workspace_id: str, profile: IndexProfileInput) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO search_index_profiles (
    id, workspace_id, backend, embedding_model, embedding_dimension,
    chunking_version, tokenizer, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(backend)s, %(embedding_model)s,
    %(embedding_dimension)s, %(chunking_version)s, %(tokenizer)s,
    %(metadata_json)s::jsonb
)
ON CONFLICT (id) DO UPDATE
SET backend = EXCLUDED.backend,
    embedding_model = EXCLUDED.embedding_model,
    embedding_dimension = EXCLUDED.embedding_dimension,
    chunking_version = EXCLUDED.chunking_version,
    tokenizer = EXCLUDED.tokenizer,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "id": profile.id,
            "workspace_id": workspace_id,
            "backend": profile.backend,
            "embedding_model": profile.embedding_model,
            "embedding_dimension": profile.embedding_dimension,
            "chunking_version": profile.chunking_version,
            "tokenizer": profile.tokenizer,
            "metadata_json": _json_param(profile.metadata or {}),
        },
    )


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
    return SqlStatement(
        sql="""
INSERT INTO transcript_versions (
    id, workspace_id, video_id, source, language_code,
    content_hash, metadata_json
)
VALUES (
    %(transcript_version_id)s, %(workspace_id)s, %(video_id)s, %(source)s,
    %(language_code)s, %(content_hash)s, %(metadata_json)s::jsonb
)
ON CONFLICT (id) DO UPDATE
SET source = EXCLUDED.source,
    language_code = EXCLUDED.language_code,
    content_hash = EXCLUDED.content_hash,
    metadata_json = EXCLUDED.metadata_json
RETURNING *;
""".strip(),
        params={
            "workspace_id": workspace_id,
            "video_id": video_id,
            "transcript_version_id": transcript_version_id,
            "source": source,
            "language_code": language_code,
            "content_hash": content_hash,
            "metadata_json": _json_param(metadata or {}),
        },
    )


def upsert_chunk_sql(
    *,
    workspace_id: str,
    hosted_video_id: str,
    transcript_version_id: str,
    index_profile_id: str,
    chunk: TranscriptChunkInput,
    chunk_id: str,
) -> SqlStatement:
    return SqlStatement(
        sql="""
INSERT INTO chunks (
    id, workspace_id, video_id, transcript_version_id, index_profile_id,
    chunk_index, start_seconds, end_seconds, text, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(video_id)s, %(transcript_version_id)s,
    %(index_profile_id)s, %(chunk_index)s, %(start_seconds)s, %(end_seconds)s,
    %(text)s, %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, transcript_version_id, index_profile_id, chunk_index) DO UPDATE
SET start_seconds = EXCLUDED.start_seconds,
    end_seconds = EXCLUDED.end_seconds,
    text = EXCLUDED.text,
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
    return SqlStatement(
        sql="""
INSERT INTO job_operations (
    id, workspace_id, job_id, operation, source_id, video_id, input_hash,
    idempotency_key, status, attempt_count, usage_reservation_id, metadata_json
)
VALUES (
    %(id)s, %(workspace_id)s, %(job_id)s, %(operation)s, %(source_id)s,
    %(video_id)s, %(input_hash)s, %(idempotency_key)s, %(status)s,
    %(attempt_count)s, %(usage_reservation_id)s, %(metadata_json)s::jsonb
)
ON CONFLICT (workspace_id, idempotency_key) DO UPDATE
SET status = CASE
        WHEN job_operations.status IN ('denied', 'succeeded', 'failed_final', 'reconciled', 'released')
         AND EXCLUDED.status IN ('planned', 'reserved', 'started')
        THEN job_operations.status
        ELSE EXCLUDED.status
    END,
    attempt_count = job_operations.attempt_count,
    usage_reservation_id = EXCLUDED.usage_reservation_id,
    metadata_json = EXCLUDED.metadata_json,
    updated_at = now()
RETURNING *;
""".strip(),
        params={
            "id": operation.id,
            "workspace_id": operation.workspace_id,
            "job_id": operation.job_id,
            "operation": operation.operation,
            "source_id": operation.source_id,
            "video_id": operation.video_id,
            "input_hash": operation.input_hash,
            "idempotency_key": operation.idempotency_key,
            "status": operation.status,
            "attempt_count": operation.attempt_count,
            "usage_reservation_id": operation.metadata_jsonb.get("usage_reservation_id") or None,
            "metadata_json": _json_param(operation.metadata_jsonb),
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
            "id": _stable_id("res", workspace_id, reservation_key),
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
            mode="hosted",
            model_or_plan=DEFAULT_EMBEDDING_MODEL,
        ),
        default_search_store_allocation(workspace_id=workspace_id, operation="*", index_profile_ref=index_profile_id),
    )


def _default_search_query(chunks: Sequence[TranscriptChunkInput]) -> str:
    first = chunks[0].text.strip().split()
    return " ".join(first[: min(3, len(first))]) or "transcript"


def _chunk_hash_payload(chunk: TranscriptChunkInput) -> dict[str, Any]:
    return {
        "chunk_index": chunk.chunk_index,
        "start_seconds": chunk.start_seconds,
        "end_seconds": chunk.end_seconds,
        "text": chunk.text,
        "metadata": dict(chunk.metadata or {}),
    }


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


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in vector) + "]"


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


__all__ = [
    "DEFAULT_CHUNKING_VERSION",
    "DEFAULT_EMBEDDING_DIMENSION",
    "DEFAULT_EMBEDDING_MODEL",
    "DEFAULT_INDEX_PROFILE_ID",
    "HostedIndexingPlan",
    "HostedIndexingExecutionResult",
    "HostedIndexingExecutor",
    "HostedVideoInput",
    "IndexProfileInput",
    "PlannedSqlOperation",
    "REAL_HOSTED_CHUNKING_VERSION",
    "TranscriptChunkInput",
    "extract_public_youtube_video_id",
    "mock_embedding_vector",
    "plan_mock_hosted_public_indexing",
    "plan_real_hosted_public_indexing",
    "source_from_public_youtube_input",
    "upsert_chunk_embedding_sql",
    "upsert_chunk_sql",
    "upsert_index_profile_sql",
    "upsert_job_operation_sql",
    "upsert_video_sql",
]
