from __future__ import annotations

from collections.abc import Iterator
import os
import shutil
import subprocess
import time

import pytest


VECTORCHORD_IMAGE = "tensorchord/vchord-suite:pg17-latest"


@pytest.fixture(scope="session")
def vectorchord_dsn() -> Iterator[str]:
    configured = os.getenv("YUTOME_TEST_VECTORCHORD_DSN")
    if configured:
        _wait_for_postgres(configured)
        yield configured
        return
    if os.getenv("YUTOME_TEST_VECTORCHORD_AUTO") != "1":
        pytest.skip("set YUTOME_TEST_VECTORCHORD_DSN or YUTOME_TEST_VECTORCHORD_AUTO=1 for VectorChord live tests")
    if not shutil.which("docker"):
        pytest.skip("Docker/OrbStack is required for VectorChord live tests")
    if subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
        pytest.skip("Docker/OrbStack is not running")

    container_name = f"yutome-vchord-test-{os.getpid()}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=yutome_vector_test",
            "-p",
            "127.0.0.1::5432",
            VECTORCHORD_IMAGE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if started.returncode != 0:
        pytest.skip(f"could not start {VECTORCHORD_IMAGE}: {started.stderr.strip()}")
    try:
        port = (
            subprocess.check_output(["docker", "port", container_name, "5432/tcp"], text=True)
            .strip()
            .rsplit(":", 1)[1]
        )
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/yutome_vector_test"
        _wait_for_postgres(dsn)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for_postgres(dsn: str) -> None:
    import psycopg

    last_error: BaseException | None = None
    for _ in range(80):
        try:
            with psycopg.connect(dsn) as connection:
                connection.execute("SELECT 1;")
            return
        except psycopg.OperationalError as exc:
            last_error = exc
            time.sleep(0.25)
    raise AssertionError("could not connect to VectorChord test DSN") from last_error


def test_vectorchord_suite_executes_bm25_and_vector_sql(vectorchord_dsn: str) -> None:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(vectorchord_dsn, autocommit=True, row_factory=dict_row) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        connection.execute("CREATE EXTENSION IF NOT EXISTS vchord;")
        connection.execute("CREATE EXTENSION IF NOT EXISTS pg_tokenizer;")
        connection.execute("CREATE EXTENSION IF NOT EXISTS vchord_bm25;")
        connection.execute(
            """
            DO $yutome$
            BEGIN
                BEGIN
                    PERFORM create_tokenizer('yutome_test_llmlingua2', $$ model = "llmlingua2" $$);
                EXCEPTION WHEN OTHERS THEN
                    IF SQLERRM LIKE 'Tokenizer already exists:%%' THEN
                        NULL;
                    ELSE
                        RAISE;
                    END IF;
                END;
            END
            $yutome$;
            """
        )
        connection.execute("DROP TABLE IF EXISTS yutome_vectorchord_smoke;")
        connection.execute(
            """
            CREATE TABLE yutome_vectorchord_smoke (
                id text PRIMARY KEY,
                content text NOT NULL,
                bm25_document bm25vector,
                embedding vector(3) NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO yutome_vectorchord_smoke (id, content, embedding)
            VALUES
                ('brain', 'brain health and cognitive function', '[0.1,0.2,0.3]'::vector(3)),
                ('muscle', 'resistance training and muscle protein', '[0.9,0.1,0.1]'::vector(3));
            """
        )
        connection.execute(
            """
            UPDATE yutome_vectorchord_smoke
            SET bm25_document = tokenize(content, 'yutome_test_llmlingua2')::bm25vector;
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_yutome_vectorchord_smoke_bm25
                ON yutome_vectorchord_smoke USING bm25 (bm25_document bm25_ops);
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_yutome_vectorchord_smoke_embedding
                ON yutome_vectorchord_smoke USING vchordrq (embedding vector_l2_ops);
            """
        )

        lexical = connection.execute(
            """
            SELECT id,
                   bm25_document <&> to_bm25query(
                       'idx_yutome_vectorchord_smoke_bm25'::regclass,
                       tokenize('brain cognitive', 'yutome_test_llmlingua2')::bm25vector
                   ) AS score
            FROM yutome_vectorchord_smoke
            ORDER BY score ASC, id
            LIMIT 1;
            """
        ).fetchone()
        semantic = connection.execute(
            """
            SELECT id, embedding <-> '[0.1,0.2,0.3]'::vector(3) AS distance
            FROM yutome_vectorchord_smoke
            ORDER BY distance ASC, id
            LIMIT 1;
            """
        ).fetchone()

        assert lexical["id"] == "brain"
        assert semantic["id"] == "brain"
