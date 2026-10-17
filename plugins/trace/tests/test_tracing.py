
"""v4 migration test - code-only, no Langfuse project access.

Stands up a LOCAL OTLP collector, points tracing.py at it with dummy credentials, runs a trace, then
decodes the protobuf the exporter actually sent and asserts the v4 contract:
  1. transport is /api/public/otel/v1/traces
  2. the x-langfuse-ingestion-version: 4 header is present   (v4 requirement)
  3. every span carries the correlating attributes           (v4: filterable on children)
  4. the ROOT observation carries the overall input AND output (v4: no trace-level I/O)
  5. nothing is sent to the legacy /api/public/ingestion
"""
import base64, http.server, json, os, sys, threading

CAPTURED = []
COLLECTOR = ("127.0.0.1", 4319)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        CAPTURED.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": self.rfile.read(n)})
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
        payload = trace_service_pb2.ExportTraceServiceResponse().SerializeToString()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(COLLECTOR, Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

os.environ["LANGFUSE_HOST"] = "http://127.0.0.1:4319"
os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-test"
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-test"
os.environ["PRIME_AGENT_TRACE_DIR"] = "/tmp/lf_test_traces"

HERE = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # factory/
import tracing
tracing.CONFIG = HERE.parent / "langfuse.json"   # real creds are ignored: env wins, and it is git-ignored   # no real creds leak in

trace_id = tracing.start_trace("langfuse-v4-migration-test", session_id="sess-abc", user_id="randyt",
                               input={"brief": "migrate to v4"}, tags=["factory", "test"],
                               version="0.10.0", metadata={"run": "77df3e"})
with tracing.span("planner", model="openrouter/x") as sp:
    sp.attr(answer_len=12)
tracing.event("decision", question="q", choice="a", confidence=0.94)
tracing.end_trace(output={"spec_path": "/tmp/spec.md"})
tracing.flush()
srv.shutdown()

assert CAPTURED, "no OTLP request reached the collector"
req = CAPTURED[0]
checks = []
def check(name, ok, detail=""):
    checks.append((name, ok, detail))

check("transport path", req["path"] == "/api/public/otel/v1/traces", req["path"])
check("v4 ingestion header", req["headers"].get("x-langfuse-ingestion-version") == "4",
      str(req["headers"].get("x-langfuse-ingestion-version")))
check("basic auth present", req["headers"].get("authorization", "").startswith("Basic "))
check("no legacy ingestion call", all("/api/public/ingestion" not in c["path"] for c in CAPTURED),
      str([c["path"] for c in CAPTURED]))

from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
export = trace_service_pb2.ExportTraceServiceRequest()
export.ParseFromString(req["body"])
spans = [s for rs in export.resource_spans for ss in rs.scope_spans for s in ss.spans]
check("spans exported", len(spans) >= 3, f"{len(spans)} spans")

def attrs_of(span):
    return {kv.key: (kv.value.string_value or kv.value.int_value or kv.value.double_value
                     or kv.value.bool_value or str(kv.value.array_value))
            for kv in span.attributes}

all_attrs = [attrs_of(s) for s in spans]
sess = [a.get("langfuse.session.id") for a in all_attrs]
user = [a.get("langfuse.user.id") for a in all_attrs]
names = [a.get("langfuse.trace.name") for a in all_attrs]
check("session.id on EVERY span", all(s == "sess-abc" for s in sess), str(sess))
check("user.id on EVERY span", all(u == "randyt" for u in user), str(user))
check("trace.name on EVERY span", all(n == "langfuse-v4-migration-test" for n in names), str(names))
check("version/environment propagated",
      all(a.get("langfuse.version") == "0.10.0" and a.get("langfuse.environment") for a in all_attrs))

root = next((s for s in spans if s.name == "langfuse-v4-migration-test"), None)
check("root observation exists", root is not None, str([s.name for s in spans]))
ra = attrs_of(root) if root is not None else {}
check("root has observation.input", "langfuse.observation.input" in ra, json.dumps(ra)[:120])
check("root has observation.output", ra.get("langfuse.observation.output", "") == json.dumps({"spec_path": "/tmp/spec.md"}),
      str(ra.get("langfuse.observation.output")))
check("no legacy trace.input/output", not any(k.startswith("langfuse.trace.input") or k.startswith("langfuse.trace.output")
                                             for a in all_attrs for k in a))
check("observation type set", ra.get("langfuse.observation.type") == "span", str(ra.get("langfuse.observation.type")))

print("\n=== v4 migration test ===")
for name, ok, detail in checks:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if not ok else ""))
failed = [c for c in checks if not c[1]]
print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
sys.exit(1 if failed else 0)
