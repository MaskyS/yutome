# Hosted SQL: SQLAlchemy Core conventions

How hosted query/command SQL is built, and the rules an agent should follow when adding or
changing it. The hosted backend builds SQL with **SQLAlchemy Core** and compiles it to a
parameterized `SqlStatement` (raw SQL string + psycopg named params) via
[`compile_postgres_statement`](../../src/yutome/hosted/sqlalchemy_core.py). Column references are
type-checked against [`schema.py`](../../src/yutome/hosted/schema.py) — a wrong column raises at
build time instead of failing as a runtime SQL error.

## The idiom

```python
from sqlalchemy import select            # + insert/update/func/cast/bindparam as needed
from sqlalchemy.dialects.postgresql import insert   # INSERT ... ON CONFLICT
from yutome.hosted.schema import jobs, sources
from yutome.hosted.sqlalchemy_core import compile_postgres_statement
from yutome.hosted.repositories import SqlStatement

def my_query_sql(*, workspace_id: str) -> SqlStatement:
    statement = (
        select(jobs.c.id, sources.c.display_name.label("source_display_name"))
        .select_from(jobs.outerjoin(sources, sources.c.id == jobs.c.source_id))
        .where(jobs.c.workspace_id == workspace_id)
        .order_by(jobs.c.created_at.desc())
        .limit(50)
    )
    sql, params = compile_postgres_statement(statement)
    return SqlStatement(sql=sql + ";", params=params)
```

Reference implementations: `account_jobs_sql` ([source_import.py](../../src/yutome/hosted/source_import.py)
— SELECT with outer joins + JSONB), the `upsert_*` builders in
[billing.py](../../src/yutome/hosted/billing.py) (INSERT … ON CONFLICT), and
[repositories.py](../../src/yutome/hosted/repositories.py).

## JSONB

- **Read**: `t.c.col["key"].astext` → `col ->> 'key'`; `t.c.col["key"]` → `col -> 'key'`.
- **Write a plain JSONB column value**: pass the JSON **string** (`_json_param(...)`), **no cast** —
  the column is JSONB, so Postgres coerces the bound value. (Confirmed by the live-Postgres-tested
  billing upserts and the SQLAlchemy PostgreSQL docs: a cast is not needed when the column is
  already JSONB.)
- **JSONB as an operator operand** (`col || $param`): the bound value **must** be cast —
  `cast(_json_param(x), JSONB)`. Not needed when the other operand is `EXCLUDED.col` (already
  JSONB-typed), e.g. `t.c.metadata_json.op("||")(statement.excluded.metadata_json)`.
- **Do not pass a Python dict** for a JSONB value: `compile_postgres_statement` extracts raw params
  *before* type bind-processing, so psycopg would receive an un-adaptable `dict`. Always serialize
  to a string first (`_json_param`).

## Parameters

- Pass Python values directly; SQLAlchemy **auto-names** params (`workspace_id_1`).
  `SqlStatement.params` is handed **wholesale** to `connection.execute(sql, params)`.
- **Never** read an individual param by key in production code, and **never** assert on `%(name)s`
  SQL text or on param-key names in tests. Assert on **behavior/output** and on **bound values**
  (`statement.params.values()`). The raw→Core sweep changed param keys; tests that coupled to the
  old `%(name)s`/keys were updated to value/behavior assertions.
- Nullable filters: branch in Python (omit the `.where(...)` when the value is `None`) instead of
  `%(x)s IS NULL OR col = %(x)s`.

## Leave RAW (do not convert to Core)

Convert only where Core **earns its keep**: multi-table joins, dynamic/conditional filters,
`on_conflict` upserts, JSONB manipulation, or `ANY`/array params. For everything below, the raw
parameterized SQL is clearer — keep it:

- **Trivial static statements** — a fixed single-table `SELECT` or `UPDATE` of a few columns.
  Wrapping one in a Core builder (plus a `_sql_statement` helper) turns a clear ~8-line `%(name)s`
  SQL constant into ~16 lines of indirection for ~zero benefit (type-checking a handful of stable
  column names doesn't pay for it). Leave these raw.
- **VectorChord / full-text** — bm25, `bm25vector`, `tokenize`, vector ops (`<=>`, `<->`):
  `search_store.py` and the chunk/embedding writes in `indexing.py`.
- **DDL** — `CREATE`/`ALTER`/`DROP`/`CREATE INDEX`: `migrations.py`, schema constant SQL.
- **Concurrency locks** — `FOR UPDATE` / `FOR NO KEY UPDATE` / `SKIP LOCKED` / `pg_advisory_*`:
  balance/reservation locking (ledger.py), job claiming (jobs.py), source-refresh & maintenance
  ticks (runtime.py), billing-export claim (billing.py). Clearer and safer as raw SQL, and already
  tested; the Core translation risk (double-spend, lost leases, deadlocks) outweighs the benefit.
- **Heavy aggregations** — multi-table `jsonb_agg` / correlated-subquery snapshots, e.g.
  `billing_debug_snapshot_sql` (billing.py).

## Validate

- Build-time: import the builder and print `.sql` — a wrong column raises `AttributeError`.
- Real Postgres: [`tests/test_hosted_postgres.py`](../../tests/test_hosted_postgres.py) executes
  representative generated SQL against a disposable Postgres (auto-started `postgres:16-alpine`, or
  set `YUTOME_TEST_POSTGRES_DSN`). Add a case there for new or load-bearing queries. See the
  Postgres-diagnostics note in [`CLAUDE.md`](../../CLAUDE.md).

## Sources

- SQLAlchemy 2.0 — PostgreSQL dialect (INSERT … ON CONFLICT, JSON/JSONB):
  <https://docs.sqlalchemy.org/en/20/dialects/postgresql.html>
- SQLAlchemy 2.0 — selecting & operators (`select()`, `.op()`, casts):
  <https://docs.sqlalchemy.org/en/20/core/sqlelement.html>
- `on_conflict_do_update` with a JSONB column (bind/cast behavior):
  <https://groups.google.com/g/sqlalchemy/c/S-mRpZD4ED4>
