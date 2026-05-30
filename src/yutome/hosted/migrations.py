from __future__ import annotations


HOSTED_VECTOR_BACKEND = "postgres_vectorchord"
HOSTED_DEFAULT_EMBEDDING_MODEL = "voyage-4-lite"
HOSTED_DEFAULT_EMBEDDING_DIMENSION = 1024
HOSTED_DEFAULT_TOKENIZER = "yutome_llmlingua2"
HOSTED_VECTOR_INDEX_METHOD = "vchordrq"


POSTGRES_PHASE1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    email text,
    normalized_email text,
    name text,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS normalized_email text;

UPDATE users
SET normalized_email = lower(btrim(email))
WHERE normalized_email IS NULL
  AND email IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_normalized_email
    ON users(normalized_email);

CREATE TABLE IF NOT EXISTS workspaces (
    id text PRIMARY KEY,
    owner_user_id text REFERENCES users(id),
    name text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    subscription_status text NOT NULL DEFAULT 'trialing',
    trial_ends_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workspace_members (
    workspace_id text NOT NULL REFERENCES workspaces(id),
    user_id text NOT NULL REFERENCES users(id),
    role text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS account_sessions (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id),
    workspace_id text NOT NULL REFERENCES workspaces(id),
    session_hash text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
    audience text,
    client_id text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_account_sessions_session_hash
    ON account_sessions(session_hash);

CREATE TABLE IF NOT EXISTS provider_allocations (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    provider text NOT NULL,
    operation text NOT NULL,
    credential_mode text NOT NULL,
    status text NOT NULL DEFAULT 'active',
    model_or_plan text,
    external_allocation_id text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service_allocations (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    service text NOT NULL,
    operation text NOT NULL,
    credential_mode text NOT NULL DEFAULT 'service_internal',
    status text NOT NULL DEFAULT 'active',
    backend text NOT NULL,
    index_profile_ref text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS usage_reservations (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    subject text NOT NULL,
    operation text NOT NULL,
    allocation_id text,
    credential_mode text NOT NULL,
    estimated_units_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    idempotency_key text NOT NULL,
    status text NOT NULL,
    decision_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id text PRIMARY KEY,
    reservation_id text REFERENCES usage_reservations(id),
    workspace_id text NOT NULL REFERENCES workspaces(id),
    subject text NOT NULL,
    operation text NOT NULL,
    event_type text NOT NULL,
    status text NOT NULL,
    actual_units_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    provider_request_id text,
    error_code text,
    raw_usage_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_usage_events_workspace_created
    ON usage_events(workspace_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_events_subject_operation
    ON usage_events(subject, operation, created_at DESC);
"""


POSTGRES_PHASE4_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS vchord;
CREATE EXTENSION IF NOT EXISTS pg_tokenizer;
CREATE EXTENSION IF NOT EXISTS vchord_bm25;
DO $yutome$ BEGIN BEGIN PERFORM create_tokenizer('yutome_llmlingua2', $$ model = "llmlingua2" $$); EXCEPTION WHEN OTHERS THEN IF SQLERRM LIKE 'Tokenizer already exists:%%' THEN NULL; ELSE RAISE; END IF; END; END $yutome$;
DO $yutome$ BEGIN BEGIN PERFORM create_tokenizer('pg_tokenizer', $$ model = "llmlingua2" $$); EXCEPTION WHEN OTHERS THEN IF SQLERRM LIKE 'Tokenizer already exists:%%' THEN NULL; ELSE RAISE; END IF; END; END $yutome$;

CREATE TABLE IF NOT EXISTS account_grants (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id),
    workspace_id text NOT NULL REFERENCES workspaces(id),
    kind text NOT NULL,
    scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
    status text NOT NULL DEFAULT 'active',
    audience text,
    client_id text,
    install_id text,
    token_version integer NOT NULL DEFAULT 1,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS youtube_grants (
    id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES users(id),
    workspace_id text NOT NULL REFERENCES workspaces(id),
    scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
    status text NOT NULL DEFAULT 'active',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS sources (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    source_type text NOT NULL,
    source_url text NOT NULL,
    canonical_channel_id text,
    canonical_playlist_id text,
    canonical_video_id text,
    display_name text,
    selected boolean NOT NULL DEFAULT true,
    auto_index_allowed boolean NOT NULL DEFAULT true,
    import_source text NOT NULL,
    auth_grant_id text REFERENCES youtube_grants(id),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'active',
    last_discovered_at timestamptz,
    last_indexed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, source_url)
);

CREATE TABLE IF NOT EXISTS source_refresh_policies (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    source_id text NOT NULL REFERENCES sources(id),
    enabled boolean NOT NULL DEFAULT true,
    cadence_seconds integer NOT NULL DEFAULT 900,
    jitter_seconds integer NOT NULL DEFAULT 0,
    next_run_at timestamptz NOT NULL,
    last_started_at timestamptz,
    last_succeeded_at timestamptz,
    cursor_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    max_new_videos_per_run integer,
    max_index_jobs_per_day integer,
    policy_snapshot_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_code text,
    failure_message text,
    locked_by text,
    locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, source_id)
);

CREATE TABLE IF NOT EXISTS jobs (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    source_id text REFERENCES sources(id),
    job_type text NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    priority integer NOT NULL DEFAULT 100,
    idempotency_key text NOT NULL,
    run_after timestamptz,
    executor_kind text,
    executor_ref text,
    lease_owner text,
    leased_at timestamptz,
    lease_expires_at timestamptz,
    retry_after timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    cancelled_at timestamptz,
    error_code text,
    error_message text,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE(workspace_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS job_operations (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    job_id text NOT NULL REFERENCES jobs(id),
    operation text NOT NULL,
    source_id text REFERENCES sources(id),
    video_id text,
    input_hash text NOT NULL,
    idempotency_key text NOT NULL,
    status text NOT NULL DEFAULT 'planned',
    attempt_count integer NOT NULL DEFAULT 0,
    usage_reservation_id text REFERENCES usage_reservations(id),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, idempotency_key)
);

ALTER TABLE job_operations
    ADD COLUMN IF NOT EXISTS output_json jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS videos (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    source_id text REFERENCES sources(id),
    youtube_video_id text NOT NULL,
    active_transcript_version_id text,
    channel_id text,
    title text NOT NULL DEFAULT '',
    description text NOT NULL DEFAULT '',
    published_at timestamptz,
    duration_seconds integer,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, youtube_video_id)
);

CREATE TABLE IF NOT EXISTS transcript_versions (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    video_id text NOT NULL REFERENCES videos(id),
    source text NOT NULL,
    language_code text,
    content_hash text NOT NULL,
    provider_request_id text,
    usage_event_id text REFERENCES usage_events(id),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_index_profiles (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    backend text NOT NULL DEFAULT '%(HOSTED_VECTOR_BACKEND)s',
    embedding_model text NOT NULL DEFAULT '%(HOSTED_DEFAULT_EMBEDDING_MODEL)s',
    embedding_dimension integer NOT NULL DEFAULT %(HOSTED_DEFAULT_EMBEDDING_DIMENSION)d,
    chunking_version text NOT NULL,
    tokenizer text NOT NULL DEFAULT '%(HOSTED_DEFAULT_TOKENIZER)s',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_search_index_profiles_embedding_dimension_supported
        CHECK (embedding_dimension = %(HOSTED_DEFAULT_EMBEDDING_DIMENSION)d),
    UNIQUE(workspace_id, backend, embedding_model, embedding_dimension, chunking_version, tokenizer)
);

CREATE TABLE IF NOT EXISTS chunks (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    video_id text NOT NULL REFERENCES videos(id),
    transcript_version_id text NOT NULL REFERENCES transcript_versions(id),
    index_profile_id text NOT NULL REFERENCES search_index_profiles(id),
    chunk_index integer NOT NULL,
    start_seconds numeric,
    end_seconds numeric,
    text text NOT NULL,
    bm25_document bm25vector NOT NULL,
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(workspace_id, transcript_version_id, index_profile_id, chunk_index)
);

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS bm25_document bm25vector;

UPDATE chunks
SET bm25_document = tokenize(chunks.text, sip.tokenizer)::bm25vector
FROM search_index_profiles sip
WHERE chunks.index_profile_id = sip.id
  AND chunks.workspace_id = sip.workspace_id
  AND chunks.bm25_document IS NULL;

ALTER TABLE chunks
    ALTER COLUMN bm25_document SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_chunks_bm25_document
    ON chunks USING bm25 (bm25_document bm25_ops);

CREATE TABLE IF NOT EXISTS chunk_embeddings (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    chunk_id text NOT NULL REFERENCES chunks(id),
    index_profile_id text NOT NULL REFERENCES search_index_profiles(id),
    embedding vector(%(HOSTED_DEFAULT_EMBEDDING_DIMENSION)d) NOT NULL,
    usage_event_id text REFERENCES usage_events(id),
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_chunk_embeddings_embedding_dimension
        CHECK (vector_dims(embedding) = %(HOSTED_DEFAULT_EMBEDDING_DIMENSION)d),
    UNIQUE(workspace_id, chunk_id, index_profile_id)
);

CREATE INDEX IF NOT EXISTS idx_account_grants_workspace_status
    ON account_grants(workspace_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_account_grants_install_id
    ON account_grants(install_id)
    WHERE install_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_sources_workspace_status
    ON sources(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_source_refresh_due
    ON source_refresh_policies(workspace_id, next_run_at)
    WHERE enabled = true;
CREATE INDEX IF NOT EXISTS idx_jobs_claimable
    ON jobs(priority ASC, run_after ASC NULLS FIRST, created_at ASC)
    WHERE status IN ('queued', 'retry_wait');
CREATE INDEX IF NOT EXISTS idx_jobs_workspace_status
    ON jobs(workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_job_operations_job_status
    ON job_operations(job_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_videos_workspace_channel
    ON videos(workspace_id, channel_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_videos_active_transcript
    ON videos(workspace_id, active_transcript_version_id)
    WHERE active_transcript_version_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_workspace_video
    ON chunks(workspace_id, video_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_workspace_profile
    ON chunk_embeddings(workspace_id, index_profile_id);
CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_embedding_vchordrq
    ON chunk_embeddings USING %(HOSTED_VECTOR_INDEX_METHOD)s (embedding vector_l2_ops);
""" % {
    "HOSTED_DEFAULT_EMBEDDING_DIMENSION": HOSTED_DEFAULT_EMBEDDING_DIMENSION,
    "HOSTED_DEFAULT_EMBEDDING_MODEL": HOSTED_DEFAULT_EMBEDDING_MODEL,
    "HOSTED_DEFAULT_TOKENIZER": HOSTED_DEFAULT_TOKENIZER,
    "HOSTED_VECTOR_BACKEND": HOSTED_VECTOR_BACKEND,
    "HOSTED_VECTOR_INDEX_METHOD": HOSTED_VECTOR_INDEX_METHOD,
}


# Email magic-link sign-in tokens for the web dashboard. A verified, single-use
# token is the only way the dashboard mints a session (see /account/login/* in
# http_api.py); only the token hash is stored, never the raw token.
POSTGRES_AUTH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS email_login_tokens (
    id text PRIMARY KEY,
    token_hash text NOT NULL,
    normalized_email text NOT NULL,
    name text,
    workspace_name text,
    redirect_path text,
    user_agent text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    consumed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_email_login_tokens_token_hash
    ON email_login_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_email_login_tokens_email
    ON email_login_tokens(normalized_email, created_at DESC);
CREATE TABLE IF NOT EXISTS api_keys (
    id text PRIMARY KEY,
    workspace_id text NOT NULL REFERENCES workspaces(id),
    user_id text NOT NULL REFERENCES users(id),
    key_hash text NOT NULL,
    name text,
    scopes text[] NOT NULL DEFAULT ARRAY[]::text[],
    status text NOT NULL DEFAULT 'active',
    metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    expires_at timestamptz,
    revoked_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_key_hash
    ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_workspace_status
    ON api_keys(workspace_id, status);
"""


POSTGRES_HOSTED_SCHEMA_SQL = (
    POSTGRES_PHASE1_SCHEMA_SQL + "\n" + POSTGRES_PHASE4_SCHEMA_SQL + "\n" + POSTGRES_AUTH_SCHEMA_SQL
)
