from __future__ import annotations

from yutome.hosted.postgres import (
    apply_hosted_schema,
    apply_phase1_schema,
    hosted_schema_statements,
    phase1_schema_statements,
    phase4_schema_statements,
)


class RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> object:
        self.statements.append(statement)
        return None


def test_phase1_schema_statements_include_core_usage_tables() -> None:
    statements = phase1_schema_statements()
    joined = "\n".join(statements)

    assert "CREATE TABLE IF NOT EXISTS workspaces" in joined
    assert "CREATE TABLE IF NOT EXISTS provider_allocations" in joined
    assert "CREATE TABLE IF NOT EXISTS service_allocations" in joined
    assert "CREATE TABLE IF NOT EXISTS usage_reservations" in joined
    assert "CREATE TABLE IF NOT EXISTS usage_events" in joined
    assert all(statement.endswith(";") for statement in statements)


def test_phase4_schema_statements_include_hosted_runtime_tables_and_extensions() -> None:
    statements = phase4_schema_statements()
    joined = "\n".join(statements)

    assert "CREATE EXTENSION IF NOT EXISTS vector" in joined
    assert "CREATE EXTENSION IF NOT EXISTS vchord" in joined
    assert "CREATE EXTENSION IF NOT EXISTS pg_tokenizer" in joined
    assert "CREATE EXTENSION IF NOT EXISTS vchord_bm25" in joined
    assert "create_tokenizer('yutome_llmlingua2'" in joined
    assert "create_tokenizer('pg_tokenizer'" in joined
    assert "Tokenizer already exists:%" in joined
    assert "CREATE TABLE IF NOT EXISTS sources" in joined
    assert "CREATE TABLE IF NOT EXISTS source_refresh_policies" in joined
    assert "CREATE TABLE IF NOT EXISTS jobs" in joined
    assert "CREATE TABLE IF NOT EXISTS job_operations" in joined
    assert "output_json jsonb NOT NULL DEFAULT '{}'::jsonb" in joined
    assert "ADD COLUMN IF NOT EXISTS output_json" in joined
    assert "CREATE TABLE IF NOT EXISTS search_index_profiles" in joined
    assert "embedding_model text NOT NULL DEFAULT 'voyage-4-lite'" in joined
    assert "embedding_dimension integer NOT NULL DEFAULT 1024" in joined
    assert "chk_search_index_profiles_embedding_dimension_supported" in joined
    assert "CHECK (embedding_dimension = 1024)" in joined
    assert "CREATE TABLE IF NOT EXISTS chunks" in joined
    assert "bm25_document bm25vector NOT NULL" in joined
    assert "tokenize(chunks.text, sip.tokenizer)::bm25vector" in joined
    assert "idx_chunks_bm25_document" in joined
    assert "ON chunks USING bm25 (bm25_document bm25_ops)" in joined
    assert "CREATE TABLE IF NOT EXISTS chunk_embeddings" in joined
    assert "embedding vector(1024) NOT NULL" in joined
    assert "chk_chunk_embeddings_embedding_dimension" in joined
    assert "CHECK (vector_dims(embedding) = 1024)" in joined
    assert "ON chunk_embeddings USING vchordrq (embedding vector_l2_ops)" in joined
    assert "active_transcript_version_id text" in joined
    assert "is_active boolean" not in joined
    assert "idx_active_transcript_per_video" not in joined
    assert "idx_videos_active_transcript" in joined
    assert "UNIQUE(workspace_id, idempotency_key)" in joined
    assert "idx_jobs_claimable" in joined
    assert "idx_source_refresh_due" in joined
    assert all(statement.endswith(";") for statement in statements)


def test_hosted_schema_combines_usage_and_runtime_tables_in_order() -> None:
    statements = hosted_schema_statements()
    joined = "\n".join(statements)

    assert joined.index("CREATE TABLE IF NOT EXISTS usage_reservations") < joined.index(
        "CREATE TABLE IF NOT EXISTS jobs"
    )
    assert joined.index("CREATE TABLE IF NOT EXISTS jobs") < joined.index(
        "CREATE TABLE IF NOT EXISTS chunk_embeddings"
    )
    assert joined.index("CREATE TABLE IF NOT EXISTS usage_events") < joined.index(
        "CREATE TABLE IF NOT EXISTS billing_exports"
    )
    assert "CREATE TABLE IF NOT EXISTS price_books" in joined
    assert "CREATE TABLE IF NOT EXISTS entitlement_policies" in joined
    assert "CREATE TABLE IF NOT EXISTS workspace_balances" in joined
    assert "CREATE TABLE IF NOT EXISTS billing_customers" in joined
    assert "CREATE TABLE IF NOT EXISTS polar_webhook_snapshots" in joined


def test_apply_phase1_schema_runs_statements_in_order() -> None:
    connection = RecordingConnection()

    applied = apply_phase1_schema(connection, statements=["CREATE TABLE one;", "CREATE TABLE two;"])

    assert applied == 2
    assert connection.statements == ["CREATE TABLE one;", "CREATE TABLE two;"]


def test_apply_hosted_schema_runs_all_statements() -> None:
    connection = RecordingConnection()

    applied = apply_hosted_schema(connection, statements=["CREATE EXTENSION vector;", "CREATE TABLE jobs;"])

    assert applied == 2
    assert connection.statements == ["CREATE EXTENSION vector;", "CREATE TABLE jobs;"]
