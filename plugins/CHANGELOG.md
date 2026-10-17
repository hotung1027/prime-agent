# Changelog

## 0.10.0 - 2026-10-15

First versioned cut. One plugin per capability.

### `factory/` - multi-agent orchestration
- `machines/staged-delivery/MACHINE.md`, generated from `roles.py`: research -> plan -> implement ->
  review -> verify -> merge, with a failed review or verify looping back to implement.
- `machine_runner.py`: deterministic runner that waits for each child's session answer.
- `engine.py` + `engine.json`: the staged engine (roles, hooks, per-role routing, stage gates) and
  its configuration.
- `roles.py`: twelve role prompts with their answer keys (single source of truth).
- `routing.py`: per-role model choice from a curated pool.

### `durable_function/` - optional durable execution
- `temporal_backend.py`: durable stages over the factory engine - retries, `from_stage` resume,
  gates-only runs, cancellation that takes the spawned child down.
- `worker-rs/`: **optional Rust activity worker** for mechanical activities only - it runs no
  workflows; the durable layer itself is the Python SDK.

### `trace/` - observability
- Local JSONL timeline plus Langfuse v4 OTLP export: `x-langfuse-ingestion-version: 4`, correlation
  attributes on every span, overall input/output on the root observation, no legacy ingestion endpoint.
- `tests/test_tracing.py`: 14 checks against a local OTLP collector.

### `decision_layer/` - structured choices
- `decision.py` (choice / score / noul, escalation below a confidence threshold) and
  `decision_mcp.py` (MCP server exposing the same primitives).

### `continual_learning/` - daily reflection
- `reflect.py`: gathers a day of traces, submits one batch job, writes a dated note.

### `llm_wiki/` - knowledge-vault mirror
- `obsidian_sync.py`: idempotent mirror of the knowledge base into an Obsidian vault.

### `batch/` - offline batch submission
- `batch.py`: submit/poll for many independent items, gated so a run never blocks on it.

### `tools/` - leaf utilities
- `model_cache_refresh.py` (catalog as a cache), `model_econ.py` (cost / speed / time per task).

### Not carried over
- Duplicate implementations and filename versions; the real `langfuse.json`.
