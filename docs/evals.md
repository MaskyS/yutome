# Retrieval Evals

`yutome doctor eval` executes small retrieval benchmarks against the configured Postgres + VectorChord corpus. Evals are intentionally corpus-relative: they assert that known queries surface known videos, chunks, or terms.

## Format

```json
{
  "cases": [
    {
      "name": "crohn-probiotics",
      "query": "Crohn probiotics",
      "mode": "lexical",
      "channel": "@LeoandLongevity",
      "limit": 10,
      "expected_video_ids": ["VIDEO_ID"],
      "expected_terms": ["probiotics"]
    }
  ]
}
```

Supported expectations:

- `expected_video_ids`: every listed video id must appear in returned rows.
- `expected_chunk_ids`: every listed chunk id must appear in returned rows.
- `expected_terms`: every listed term must appear in returned titles/snippets/text fields.

Run:

```bash
uv run yutome doctor eval evals/leo-smoke.json
uv run yutome doctor eval evals/leo-smoke.json --json
```

## Use

Start with a tiny smoke suite before changing retrieval ranking, chunking, VectorChord BM25/vector schema, or query projections. Add cases only when the expected hit is stable and timestamp/source quality has been inspected.
