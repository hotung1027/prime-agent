
import asyncio, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path("/tmp")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "core"))
sys.path.insert(0, str(HERE.parent))
import bus, middleware

class _Span:
    def __init__(self, cm): self.cm, self.attrs = cm, dict(cm.attrs)
    def attr(self, **kw): self.attrs.update(kw)
class _CM:
    def __init__(self, rec, name, attrs): self.rec, self.name, self.attrs = rec, name, dict(attrs)
    def __enter__(self): self.span = _Span(self); return self.span
    def __exit__(self, *a): self.rec.append({"name": self.name, "attrs": dict(self.span.attrs)})
class FakeTracing:
    def __init__(self, explode=False): self.rec, self.explode = [], explode
    def span(self, name, **attrs):
        if self.explode: raise RuntimeError("tracer down")
        return _CM(self.rec, name, attrs)

ok = []
def check(name, cond, detail=""):
    ok.append((name, bool(cond), detail)); print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if not cond else ""))

async def main():
    # 1. one span per handler, closed with the verdict
    bus.reset(); fake = FakeTracing(); middleware.tracing = fake
    mw = middleware.Middleware().install()
    bus.on("commit")(lambda e: bus.Verdict.block("1420 lines / 9 files"))
    v = await bus.dispatch(bus.Event("commit", {"loc": 1420}, session="s1"))
    check("span opened and closed", len(fake.rec) == 1, str(fake.rec))
    a = fake.rec[0]["attrs"] if fake.rec else {}
    check("span carries the verdict", a.get("verdict") == "block", str(a))
    check("span carries the handler + delivery", a.get("handler", "").endswith("main.<locals>.<lambda>") and a.get("delivery") == "inline", str(a))
    check("span carries the reason", "1420 lines" in str(a.get("reason")), str(a))
    check("duration recorded", isinstance(a.get("duration_ms"), (int, float)), str(a.get("duration_ms")))

    # 2. redaction of secrets in event attributes
    bus.reset(); fake = FakeTracing(); middleware.tracing = fake
    mw = middleware.Middleware().install()
    bus.on("turn_end")(lambda e: bus.Verdict.allow())
    await bus.dispatch(bus.Event("turn_end", {"api_key": "abc", "note": "sk-" + "a"*30}, session="s2"))
    attrs = fake.rec[0]["attrs"]["attrs"] if fake.rec else {}
    check("secret key redacted", attrs.get("api_key") == "[redacted]", str(attrs))
    check("secret value redacted", attrs.get("note") == "[redacted]", str(attrs))

    # 3. per-handler cap
    bus.reset(); middleware.tracing = FakeTracing()
    mw = middleware.Middleware().install()
    bus.on("commit", budget=bus.Budget(max_per_turn=1))(lambda e: bus.Verdict.allow())
    await bus.dispatch(bus.Event("commit", {"n": 1}, session="s3"))
    v = await bus.dispatch(bus.Event("commit", {"n": 2}, session="s3"))
    check("per-handler cap blocks the second call", any("budget exhausted" in x.reason for x in v), str([x.reason for x in v]))

    # 4. per-root call cap
    bus.reset(); middleware.tracing = FakeTracing()
    mw = middleware.Middleware(budget=middleware.RootBudget(max_calls=1)).install()
    bus.on("commit")(lambda e: bus.Verdict.allow())
    await bus.dispatch(bus.Event("commit", {"n": 3}, session="s4"))
    v = await bus.dispatch(bus.Event("commit", {"n": 4}, session="s4"))
    check("root call cap blocks the second call", any("budget exhausted" in x.reason for x in v), str([x.reason for x in v]))

    # 5. a broken tracer must not break work
    bus.reset(); middleware.tracing = FakeTracing(explode=True)
    mw = middleware.Middleware().install()
    bus.on("commit")(lambda e: bus.Verdict.allow("still works"))
    v = await bus.dispatch(bus.Event("commit", {"n": 5}, session="s5"))
    check("tracer failure does not break the dispatch", v and v[0].verdict == "allow" and v[0].reason == "still works", str(v))

    # 6. report
    print("report:", mw.report())

asyncio.run(main())
failed = [c for c in ok if not c[1]]
print(f"\n{len(ok)-len(failed)}/{len(ok)} checks passed")
sys.exit(1 if failed else 0)
