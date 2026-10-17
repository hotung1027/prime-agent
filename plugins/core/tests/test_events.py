
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import bus
ok = []
def check(n, c, d=""):
    ok.append((n, bool(c), d)); print(f"{'PASS' if c else 'FAIL'}  {n}" + (f"   [{d}]" if not c else ""))

bus.reset()
bus.on("before_tool", when=lambda e: True)(lambda e: bus.Verdict.allow())
h = bus.handlers("tool")[0]
check("before_tool -> point tool + timing before", h.name == "tool" and h.timing == "before", f"{h.name}/{h.timing}")
bus.reset()
bus.on("preToolUse", when=lambda e: True)(lambda e: bus.Verdict.allow())
h = bus.handlers("tool")[0]
check("preToolUse (Cursor) -> tool/before", h.name == "tool" and h.timing == "before")
bus.reset()
bus.on("wrap_tool_call", when=lambda e: True)(lambda e, nxt: nxt())
h = bus.handlers("tool")[0]
check("wrap_tool_call -> tool/wrap delivery", h.name == "tool" and h.timing == "wrap" and h.delivery == "wrap")
bus.reset()
bus.on("shell", when=lambda e: True)(lambda e: bus.Verdict.allow())
h = bus.handlers("shell")[0]
import events as ev
check("catalog says shell is subprocess by default", ev.CATALOG["shell"].delivery == "subprocess")
bus.reset()
bus.on("cron:nightly")(lambda e: bus.Verdict.allow())
check("cron:<name> accepted", bus.handlers("cron")[0].name == "cron")
try:
    bus.on("nonsense")(lambda e: None); check("unknown point refused", False)
except ValueError as e: check("unknown point refused", "unknown event" in str(e))
try:
    bus.on("cron:nightly", scope="project")(lambda e: None); check("global event refused per project", False)
except ValueError as e: check("global event refused per project", "global-scope" in str(e))
try:
    bus.on("tool", timing="after_") if False else bus.on("commit", timing="wrap")(lambda e, n: n())
    check("unsupported timing refused", False)
except ValueError as e: check("unsupported timing refused", "does not support" in str(e))
# camelCase case-class names resolve to the same canonical pair
for alias, expected in (("BeforeToolUse", ("tool", "before")), ("PostModelCall", ("model", "after")),
                        ("DuringToolCall", ("tool", "wrap")), ("beforeFileEdit", ("file_edit", "before")),
                        ("BeforeCommit", ("commit", "before")), ("PreShellExecution", ("shell", "before")),
                        ("postSession", ("session", "after")), ("duringModelCall", ("model", "wrap"))):
    check(f"{alias} -> {expected[0]}/{expected[1]}", ev.resolve(alias) == expected, str(ev.resolve(alias)))

# phase-prefix equivalence: pre-/before- = before, post-/after- = after, wrap-/during- = wrap
for alias, expected in (("pre_tool", ("tool", "before")), ("before_tool", ("tool", "before")),
                        ("post_tool", ("tool", "after")), ("after_tool", ("tool", "after")),
                        ("during_tool", ("tool", "wrap")), ("wrap_tool", ("tool", "wrap")),
                        ("preModel", ("model", "before")), ("postCommit", ("commit", "after")),
                        ("preToolUse", ("tool", "before")), ("postToolUse", ("tool", "after")),
                        ("duringModel", ("model", "wrap"))):
    check(f"{alias} -> {expected[0]}/{expected[1]}", ev.resolve(alias) == expected, str(ev.resolve(alias)))

# anchors: every point states what its phase is relative to
missing = [p for p, sp in ev.CATALOG.items() if not sp.anchor]
check("every point declares an anchor", not missing, str(missing))
check("tool anchor is the tool execution", "tool execution" in ev.CATALOG["tool"].anchor)
check("model anchor is the request/response", "response" in ev.CATALOG["model"].anchor)

# after cannot block: it is normalised to escalate
import asyncio
async def after_semantics():
    bus.reset()
    bus.on("commit", timing="after", delivery="inline")(lambda e: bus.Verdict.block("too late"))
    return await bus.dispatch(bus.Event("commit", {"sha": "abc"}))
v = asyncio.run(after_semantics())
check("after-hook block becomes escalate", v[0].verdict == "escalate" and "after-hook" in v[0].reason, str((v[0].verdict, v[0].reason)))
bus.reset()

print(f"\n{sum(1 for _,c,_ in ok if c)}/{len(ok)} checks passed")
sys.exit(0 if all(c for _,c,_ in ok) else 1)
