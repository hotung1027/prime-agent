
import asyncio, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "core"))
sys.path.insert(0, str(HERE.parent.parent / "trace"))
sys.path.insert(0, str(HERE.parent))
import bus, middleware

ok = []
def check(name, cond, detail=""):
    ok.append((name, bool(cond), detail)); print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if not cond else ""))

async def main():
    # 1. timing order: before asc, wrap, after desc
    bus.reset(); order = []
    bus.on("tool", when=lambda e: True, timing="before", priority=20)(lambda e: order.append("before20") or bus.Verdict.allow())
    bus.on("tool", when=lambda e: True, timing="before", priority=10)(lambda e: order.append("before10") or bus.Verdict.allow())
    bus.on("tool", when=lambda e: True, timing="wrap", priority=50)(lambda e, nxt: order.append("wrap-in") or nxt())
    bus.on("tool", when=lambda e: True, timing="after", priority=10)(lambda e: order.append("after10") or bus.Verdict.allow())
    bus.on("tool", when=lambda e: True, timing="after", priority=20)(lambda e: order.append("after20") or bus.Verdict.allow())
    async def operation(e):
        order.append("operation"); return bus.Verdict.allow("ran")
    v = await bus.dispatch(bus.Event("tool", {"t": 1}), operation=operation)
    check("timing order before->wrap->op->after", order == ["before10", "before20", "wrap-in", "operation", "after20", "after10"], str(order))
    check("operation verdict collected", any(x.reason == "ran" for x in v), str([x.reason for x in v]))

    # 2. a before-block short-circuits: operation never runs
    bus.reset(); ran = []
    bus.on("tool", when=lambda e: True, timing="before", priority=1)(lambda e: bus.Verdict.block("no"))
    bus.on("tool", when=lambda e: True, timing="after")(lambda e: ran.append("after") or bus.Verdict.allow())
    async def op2(e): ran.append("op"); return bus.Verdict.allow()
    v = await bus.dispatch(bus.Event("tool", {"t": 2}), operation=op2)
    check("before-block stops the chain", ran == [] and v[0].verdict == "block", f"{ran} {v[0].verdict}")

    # 3. a wrap can rewrite the result
    bus.reset()
    bus.on("tool", when=lambda e: True, timing="wrap")(lambda e, nxt: bus.Verdict.block("downgraded"))
    v = await bus.dispatch(bus.Event("tool", {"t": 3}), operation=lambda e: bus.Verdict.allow("fine"))
    check("wrap can override the operation verdict", any(x.verdict == "block" and x.reason == "downgraded" for x in v), str([(x.verdict, x.reason) for x in v]))

    # 4. composed middleware: two instances, both trace, both check budgets
    bus.reset()
    class Rec:
        def __init__(self): self.spans = []
        def span(self, name, **a):
            rec = self
            class CM:
                def __enter__(self):
                    class S:
                        def attr(self, **k): pass
                    return S()
                def __exit__(self, *x): rec.spans.append(name)
            return CM()
    rec = Rec(); middleware.tracing = rec
    m1 = middleware.Middleware().install()
    m2 = middleware.Middleware().install()
    bus.on("commit")(lambda e: bus.Verdict.allow())
    await bus.dispatch(bus.Event("commit", {"i": 1}, session="sx"))
    check("two middleware instances both trace", rec.spans == ["hook:commit", "hook:commit"], str(rec.spans))
    check("tracer list has both", len(bus._TRACERS) == 2, str(len(bus._TRACERS)))
    m1.uninstall()
    check("uninstall removes only its own tracer", len(bus._TRACERS) == 1, str(len(bus._TRACERS)))
    bus.reset()

    # 5. budget checkers compose: one denies -> block
    bus.reset()
    bus.add_budget_checker(lambda e, h: True)
    bus.add_budget_checker(lambda e, h: False)
    bus.on("commit")(lambda e: bus.Verdict.allow())
    v = await bus.dispatch(bus.Event("commit", {"i": 2}, session="sy"))
    check("all checkers must allow", v[0].verdict == "block" and "budget" in v[0].reason, str(v[0].verdict))
    bus.reset()

asyncio.run(main())
failed = [c for c in ok if not c[1]]
print(f"\n{len(ok)-len(failed)}/{len(ok)} checks passed")
sys.exit(1 if failed else 0)
