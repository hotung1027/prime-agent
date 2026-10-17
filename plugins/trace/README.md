# trace - observability

- `tracing.py` - local append-only JSONL timeline (always on, offline) plus a Langfuse **v4** OTLP
  export when credentials are present.
- `tests/test_tracing.py` - 14 checks against a local OTLP collector; needs no Langfuse project.
- `langfuse.example.json` - template. The real `langfuse.json` is git-ignored.

## v4 contract (what this plugin implements)

- POST `/api/public/otel/v1/traces` with `x-langfuse-ingestion-version: 4`.
- Correlation (`session.id`, `user.id`, `trace.name`, `tags`, `version`, `release`, `environment`,
  trace metadata) copied onto **every** span: v4 queries observations directly, so root-only
  attributes are invisible when filtering children.
- Overall request/response on the **root observation** (`langfuse.observation.input`/`.output`);
  there is no trace-level I/O in v4.
- No `/api/public/ingestion`: v4 accepts those POSTs but does not make them queryable.

    python tests/test_tracing.py      # 14/14

Credentials come from the environment (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`).
