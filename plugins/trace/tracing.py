"""Local temporal trace layer + optional Langfuse export (v4-ready).

Every event is appended to a local append-only timeline (fast, offline, always on): one JSON line per
event with a monotonic sequence, wall clock, trace/span ids, name, kind and attributes. When a Langfuse
key is present the same work is ALSO exported as OpenTelemetry spans to Langfuse's OTLP endpoint.

What v4 requires, and what this module does about it:
  transport      POST /api/public/otel/v1/traces with the `x-langfuse-ingestion-version: 4` header.
                 Without that header the exporter still speaks the legacy trace model.
  no trace I/O   v4 has no separate trace input/output. The overall request goes on the ROOT
                 observation (`start_trace(input=...)` -> langfuse.observation.input) and the overall
                 response on the same span when the trace closes (`end_trace(output=...)`).
  correlation    session id, user id, trace name, tags, version, release, environment and trace
                 metadata are stamped onto EVERY span by _PropagateSpanAttributes, because v4 queries
                 observations directly: an attribute that exists only on the root span is invisible
                 when filtering or aggregating its children (including per-session cost).
  no legacy batch /api/public/ingestion is deliberately NOT used. v4 accepts those POSTs but does not
                 make them queryable, so a silent fallback to it produced false confidence; if the OTLP
                 exporter is unavailable, records stay in the local timeline and status() reports why.

Credentials (either source):
  env: LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST (default https://cloud.langfuse.com)
  file: ~/.prime/agent/pipelines/langfuse.json  {"public_key": "...", "secret_key": "...", "host": "..."}

Usage
    tracing.start_trace("run-42", session_id=run_id, user_id="randyt", input={"brief": brief},
                        tags=["factory"], version="0.10.0")
    with tracing.span("planner", model="...") as sp:
        sp.attr(answer_len=12)
    tracing.end_trace(output={"spec": spec_path})
    tracing.flush()
No third-party dependency is required: urllib + json only. The OTLP export activates only when the
OpenTelemetry SDK is importable and credentials are configured.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.request
import uuid
from pathlib import Path

PIPELINES = (Path(__file__).resolve().parent if "__file__" in globals()
            else Path(os.path.expanduser("~/.prime/agent/pipelines")))
LOCAL_DIR = Path(os.environ.get("PRIME_AGENT_TRACE_DIR") or (Path.home() / ".prime" / "agent" / "traces" / "local"))
CONFIG = PIPELINES / "langfuse.json"
FLUSH_EVERY = 20
_lock = threading.Lock()
_seq = 0
_pending: list = []
_trace_id: str | None = None
_flush_report: list = []
_providers: list = []


_tracer = None
_otel_state = {"tried": False, "error": None}

# Langfuse v4 queries OBSERVATIONS directly, so a correlating attribute that exists only on the root
# span is invisible when filtering or aggregating its children. These keys are stamped onto every
# span as it starts, and the session id reaches every cost-bearing generation that way.
TRACE_ATTR_KEYS = ("langfuse.session.id", "langfuse.user.id", "langfuse.trace.name",
                   "langfuse.trace.tags", "langfuse.version", "langfuse.release",
                   "langfuse.environment")
_trace_attrs: dict = {}
_root_span = None


def _json_io(value):
    """Structured input/output must travel as a JSON string in an OTEL attribute."""
    if value is None:
        return None
    return value if isinstance(value, str) else json.dumps(value)[:4000]


def _otel():
    """A real OTel tracer exporting to Langfuse's OTLP endpoint, or None.

    Langfuse Cloud (v4) is OTLP-only: the legacy /api/public/ingestion batch accepts POSTs but the
    data is not queryable, so traces must go through /api/public/otel/v1/traces.
    """
    global _tracer
    if _tracer is not None:
        return _tracer
    if _otel_state["tried"]:
        return None
    _otel_state["tried"] = True
    public, secret, host = _credentials()
    if not (public and secret):
        return None
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except Exception as exc:
        _otel_state["error"] = f"{type(exc).__name__}: {exc}"
        return None
    from opentelemetry.sdk.trace import SpanProcessor as _SpanProcessor

    class _PropagateSpanAttributes(_SpanProcessor):
        """Copy the trace's correlating attributes onto every span.

        v4 queries observations directly, so an attribute that exists only on the root span is
        invisible when filtering or aggregating children (per-session cost included). Doing it in a
        processor rather than at each call site means a new call site cannot forget them.
        """

        def on_start(self, span, parent_context=None):
            with _lock:
                attrs = dict(_trace_attrs)
            for key in TRACE_ATTR_KEYS:
                value = attrs.get(key)
                if value not in (None, ""):
                    span.set_attribute(key, value)
            for key, value in attrs.items():
                if key.startswith("langfuse.trace.metadata."):
                    span.set_attribute(key, value)

        def on_end(self, span):
            pass

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=None):
            return True

    auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
    provider = TracerProvider(resource=Resource.create(
        {"service.name": "prime-agent", "service.version": "0.10.0",
         "deployment.environment": os.environ.get("PRIME_AGENT_ENV", "local")}))
    _providers.append(provider)
    provider.add_span_processor(_PropagateSpanAttributes())
    # v4 REQUIRES the ingestion-version header; without it the exporter speaks the legacy trace model.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=f"{host.rstrip('/')}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {auth}",
                 "x-langfuse-ingestion-version": "4"})))
    _tracer = provider.get_tracer("prime-agent")
    return _tracer


def _flat(attrs):
    """OTel attribute values are scalars or homogeneous lists; everything else goes as JSON."""
    out = {}
    for key, value in (attrs or {}).items():
        if isinstance(value, (str, bool, int, float)) or value is None:
            out[key] = "" if value is None else value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, (str, bool, int, float)) for v in value):
            out[key] = list(value)
        else:
            out[key] = json.dumps(value)[:900]
    return out


def _credentials():
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST")
    if CONFIG.exists():
        try:
            cfg = json.loads(CONFIG.read_text())
            public = public or cfg.get("public_key")
            secret = secret or cfg.get("secret_key")
            host = host or cfg.get("host")
        except ValueError:
            pass
    def real(value):
        return bool(value) and "REPLACE" not in str(value).upper()
    if not (real(public) and real(secret)):
        public = secret = None
    return public, secret, host or "https://cloud.langfuse.com"


def status():
    public, secret, host = _credentials()
    return {"langfuse_enabled": bool(public and secret), "host": host,
            "local_dir": str(LOCAL_DIR), "pending": len(_pending),
            "trace_id": _trace_id}


def start_trace(name, *, session_id=None, user_id=None, tags=None, input=None, output=None,
                version=None, release=None, environment=None, metadata=None, **attrs):
    """Begin a named trace.

    v4 has no separate trace input/output: a trace is the group of observations sharing a trace id, so
    the overall request goes on the ROOT observation (langfuse.observation.input) and the overall
    response on the same span at the end (see end_trace). Correlation ids are recorded here and
    stamped onto every span by _PropagateSpanAttributes.
    """
    global _trace_id, _root_span, _trace_attrs
    _trace_id = uuid.uuid4().hex
    _trace_attrs = {
        "langfuse.trace.name": name,
        # default the session to the trace id so cost can always be grouped per factory run
        "langfuse.session.id": session_id or _trace_id,
        "langfuse.environment": environment or os.environ.get("PRIME_AGENT_ENV", "local"),
        "langfuse.release": release or os.environ.get("PRIME_AGENT_RELEASE", ""),
    }
    if user_id:
        _trace_attrs["langfuse.user.id"] = user_id
    if version:
        _trace_attrs["langfuse.version"] = version
    if tags:
        _trace_attrs["langfuse.trace.tags"] = [str(t) for t in tags]
    for key, value in (metadata or {}).items():
        _trace_attrs[f"langfuse.trace.metadata.{key}"] = json.dumps(value)[:900]

    tracer = _otel()
    if tracer is not None:
        _root_span = tracer.start_span(name, attributes=_flat({
            **_trace_attrs, "langfuse.observation.type": "span",
            "langfuse.observation.input": _json_io(input)}))
    event("trace", trace_name=name, **attrs)
    return _trace_id


def end_trace(output=None, error=None):
    """Close the trace by putting the overall response on the root observation."""
    global _root_span
    span = _root_span
    _root_span = None
    if span is None:
        return None
    if output is not None:
        span.set_attribute("langfuse.observation.output", _json_io(output))
    if error is not None:
        from opentelemetry.trace import Status, StatusCode
        span.set_attribute("langfuse.observation.level", "ERROR")
        span.set_status(Status(StatusCode.ERROR, str(error)[:400]))
    span.end()
    return _trace_id


def _sink(record):
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with (LOCAL_DIR / f"events-{day}.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def _emit(record):
    global _seq
    with _lock:
        _seq += 1
        record["seq"] = _seq
        record.setdefault("trace_id", _trace_id)
        record.setdefault("ts", time.time())
        _sink(record)
        # No legacy /api/public/ingestion fallback: v4 accepts those POSTs but does not make them
        # queryable, so silently queueing them produced false confidence. If the OTLP exporter is
        # unavailable the record stays in the LOCAL timeline only, and status() says why.
        if _tracer is None and _otel() is None and _otel_state.get("error"):
            record["otel_error"] = _otel_state["error"]
    return record


def event(name, kind="event", **attrs):
    tracer = _otel()
    if tracer is not None:
        span = tracer.start_span(name, attributes=_flat(attrs))
        span.end()
        attrs = {**attrs, "otel_trace_id": format(span.get_span_context().trace_id, "032x")}
    return _emit({"name": name, "kind": kind, "attributes": attrs})


class _Span:
    def __init__(self, name, **attrs):
        self.name, self.attrs, self.span_id, self.t0 = name, dict(attrs), uuid.uuid4().hex, time.monotonic()
        self._otel_span = None
        self.error = None

    def attr(self, **kwargs):
        self.attrs.update(kwargs)
        return self

    def __enter__(self):
        tracer = _otel()
        if tracer is not None:
            self._otel_span = tracer.start_span(self.name, attributes=_flat(self.attrs))
            self.attrs["otel_trace_id"] = format(self._otel_span.get_span_context().trace_id, "032x")
        _emit({"name": self.name, "kind": "span-start", "span_id": self.span_id,
               "attributes": self.attrs})
        return self

    def __exit__(self, exc_type, exc, tb):
        self.attrs["duration_ms"] = round((time.monotonic() - self.t0) * 1000, 1)
        if exc is not None:
            self.attrs["error"] = f"{exc_type.__name__}: {exc}"
        span = getattr(self, "_otel_span", None)
        if span is not None:
            from opentelemetry.trace import Status, StatusCode
            for key, value in _flat(self.attrs).items():
                span.set_attribute(key, value)
            if exc is not None:
                span.set_status(Status(StatusCode.ERROR, f"{exc_type.__name__}: {exc}"))
            span.end()
        _emit({"name": self.name, "kind": "span-end", "span_id": self.span_id,
               "attributes": self.attrs})
        return False


def span(name, **attrs):
    return _Span(name, **attrs)


def flush(verbose=False):
    """Push queued spans. OTLP is the only export path in v4."""
    if _tracer is not None:
        try:
            from opentelemetry.sdk.trace import TracerProvider
            for provider in [p for p in _providers]:
                provider.force_flush(timeout_millis=15000)
        except Exception as exc:
            _flush_report.append({"status": "otel", "error": str(exc)[:200]})
    if verbose:
        print(json.dumps({"otel": _otel_state, "report": _flush_report[-3:]}, indent=1))
    return _flush_report[-1] if _flush_report else None
