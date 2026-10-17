
import asyncio, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import bus, events as ev
ok = []
def check(n, c, d=""):
    ok.append((n, bool(c), d)); print(f"{'PASS' if c else 'FAIL'}  {n}" + (f"   [{d}]" if not c else ""))

async def main():
    # add() with an actual class object
    bus.reset(); seen = []
    def lint(e):
        seen.append((type(e).__name__, e.command, e.tool)); return bus.Verdict.allow()
    bus.add(ev.BeforeToolUse, callback=lint, when=r"^git ")
    v = await bus.dispatch(bus.Event("tool", {"tool": "bash", "command": "git commit -m x"}))
    check("class registration resolves to tool/before", bus._HANDLERS[0].name == "tool" and bus._HANDLERS[0].timing == "before")
    check("callback receives the typed event", seen == [("BeforeToolUse", "git commit -m x", "bash")], str(seen))

    # Post alias class
    bus.reset(); got = []
    bus.add(ev.PostModelCall, callback=lambda e: got.append(e.model) or bus.Verdict.allow())
    await bus.dispatch(bus.Event("model", {"model": "glm-5.3-flash", "messages": []}))
    check("PostModelCall alias works and types the payload", got == ["glm-5.3-flash"], str(got))

    # an after-class block becomes escalate
    bus.reset()
    bus.add(ev.PostCommit, callback=lambda e: bus.Verdict.block("late"))
    v = await bus.dispatch(bus.Event("commit", {"sha": "abc"}))
    check("typed after-hook block -> escalate", v[0].verdict == "escalate" and "after-hook" in v[0].reason, str(v[0].verdict))

    # wrap class
    bus.reset()
    bus.add(ev.DuringToolCall, callback=lambda e, nxt: bus.Verdict.allow("wrapped"), when=r".")
    v = await bus.dispatch(bus.Event("tool", {"command": "ls"}), operation=lambda e: bus.Verdict.allow("real"))
    check("typed wrap receives (event, next)", any(x.reason == "wrapped" for x in v), str([x.reason for x in v]))

    # string and class registration coexist; typed event still exposes attrs/text
    bus.reset(); shapes = []
    bus.add("tool", callback=lambda e: shapes.append(hasattr(e, "raw")) or bus.Verdict.allow(), when=r".")
    bus.add(ev.BeforeToolUse, callback=lambda e: shapes.append(type(e).__name__) or bus.Verdict.allow(), when=r".")
    await bus.dispatch(bus.Event("tool", {"command": "x"}))
    check("raw and typed registrations coexist", shapes == [False, "BeforeToolUse"], str(shapes))

    # the class IS the dispatch key: a typed event runs only its own phase
    bus.reset(); order = []
    bus.add("commit", timing="before", callback=lambda e: order.append("before") or bus.Verdict.allow())
    bus.add("commit", timing="after", callback=lambda e: order.append("after") or bus.Verdict.allow())
    await bus.dispatch(ev.BeforeCommit(bus.Event("commit", {"sha": "a"})))
    check("BeforeCommit runs only the before phase", order == ["before"], str(order))
    await bus.dispatch(ev.AfterCommit(bus.Event("commit", {"sha": "a"})))
    check("AfterCommit runs only the after phase", order == ["before", "after"], str(order))

    # a raw Event keeps the classic all-phases behaviour
    bus.reset(); order = []
    bus.add("commit", timing="before", callback=lambda e: order.append("before") or bus.Verdict.allow())
    bus.add("commit", timing="after", callback=lambda e: order.append("after") or bus.Verdict.allow())
    await bus.dispatch(bus.Event("commit", {"sha": "b"}))
    check("raw Event runs both phases", order == ["before", "after"], str(order))

    # emit() builds the raw event from the class
    bus.reset(); got = []
    bus.add(ev.AfterCommit, callback=lambda e: got.append(e.sha) or bus.Verdict.allow())
    await bus.emit(ev.AfterCommit, sha="c0ffee")
    check("emit() carries attributes through", got == ["c0ffee"], str(got))

asyncio.run(main())
failed = [c for c in ok if not c[1]]
print(f"\n{len(ok)-len(failed)}/{len(ok)} checks passed")
sys.exit(1 if failed else 0)
