
import asyncio, hashlib, json, os, stat, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import bus

ok = []
def check(name, cond, detail=""):
    ok.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if not cond else ""))

async def main():
    bus.reset()
    # 1. inline handler
    @bus.on("commit")
    def size_ok(e):
        return bus.Verdict.block("too big") if e.attrs.get("loc", 0) > 1000 else bus.Verdict.allow()
    v = await bus.dispatch(bus.Event("commit", {"loc": 1200}))
    check("inline blocks 1200 LOC", v and v[0].verdict == "block" and v[0].delivery == "inline", str(v))
    v = await bus.dispatch(bus.Event("commit", {"loc": 200}))
    check("inline allows 200 LOC", v and v[0].verdict == "allow")

    # 2. matcher required for tool events
    try:
        bus.on("tool_end")(lambda e: None); check("tool event without matcher refused", False)
    except ValueError as exc:
        check("tool event without matcher refused", "matcher" in str(exc), str(exc))

    # 3. depth guard
    deep = bus.Event("commit", {"loc": 1}, depth=4)
    v = await bus.dispatch(deep)
    check("depth 4 refused", v and v[0].verdict == "block" and "depth" in v[0].reason, str(v))

    # 4. dedupe within the window
    first = await bus.dispatch(bus.Event("commit", {"loc": 555}))
    second = await bus.dispatch(bus.Event("commit", {"loc": 555}))
    check("dedupe: second identical event is a no-op", len(first) == 1 and second == [], f"{len(first)}/{second}")

    # 5. subprocess: allowlisted, content-pinned (fresh registry: a before-handler from an
    #    earlier case would short-circuit the chain)
    bus.reset()
    tmp = Path(tempfile.mkdtemp())
    script = tmp / "check.sh"; script.write_text("#!/usr/bin/env bash\nread -r line\nexit 0\n")
    script.chmod(0o755)
    bus.on("commit", delivery="subprocess", script=str(script), budget=bus.Budget(timeout_s=5))(lambda e: None)
    bus.allow_script(script, hashlib.sha256(script.read_bytes()).hexdigest())
    v = await bus.dispatch(bus.Event("commit", {"loc": 70001}))
    check("subprocess runs allowlisted script", any(x.delivery == "subprocess" and x.verdict == "allow" for x in v), str([(x.delivery, x.verdict) for x in v]))
    script.write_text("#!/usr/bin/env bash\nread -r line\nexit 1\n")
    v = await bus.dispatch(bus.Event("commit", {"loc": 70002}))
    check("changed script refused by sha256", any("content changed" in x.reason for x in v), str([x.reason for x in v]))
    bus.reset()

    # 6. durable transport
    seen = []
    async def transport(event, handler):
        seen.append(event.name); return bus.Verdict.allow("started")
    bus.set_durable_transport(transport)
    bus.on("turn", timing="before", delivery="durable")(lambda e: bus.Verdict.allow())
    v = await bus.dispatch(bus.Event("turn", {"n": 1}))
    check("durable transport invoked", seen == ["turn"] and v[0].verdict == "allow", str(seen))
    bus.set_durable_transport(None)
    v = await bus.dispatch(bus.Event("turn", {"n": 2}))
    check("missing durable transport blocks loudly", v[0].verdict == "block" and "transport" in v[0].reason)
    bus.reset()

    # 7. continuation re-enters the bus
    order = []
    @bus.on("stage_end")
    def handoff(e):
        return bus.Verdict.modify("continue", bus.Continuation.emit("prompt", n=1))
    @bus.on("prompt")
    def verify(e):
        order.append("verify"); return bus.Verdict.allow()
    v = await bus.dispatch(bus.Event("stage_end"))
    check("continuation dispatches the next event", order == ["verify"], str(order))
    check("continuation keeps verdicts", any(x.verdict == "modify" for x in v))

asyncio.run(main())
failed = [c for c in ok if not c[1]]
print(f"\n{len(ok)-len(failed)}/{len(ok)} checks passed")
sys.exit(1 if failed else 0)
