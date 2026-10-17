---
name: trace-timeline
description: Trace timeline: local JSONL events + optional Langfuse export
---

Local temporal trace layer, with optional Langfuse export.

    import tracing
    tracing.status()                     # {"langfuse_enabled", "host", "local_dir", "pending"}
    tracing.start_trace("pipeline:dev", stage="dev")
    with tracing.span("role:planner", model="...") as sp:
        ...; sp.attr(items=3)            # duration_ms/error are added automatically
    tracing.event("decision", choice="SQLite", confidence=0.97)
    tracing.flush()                      # push queued events to Langfuse
Local sink (always on): ~/.prime/agent/traces/local/events-YYYY-MM-DD.jsonl - one JSON line per
event with seq, ts, trace_id, span_id, name, kind, attributes. Langfuse is opt-in via
LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY/LANGFUSE_HOST or ~/.prime/agent/pipelines/langfuse.json;
events are batched (20) to POST <host>/api/public/ingestion as span-create/span-update/trace-create,
errors get level=ERROR. Pure stdlib (urllib). The pipeline engine already traces roles, run start/end.
Never let tracing break the work, and never print credentials.
