
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
check("shell defaults to subprocess delivery", h.delivery == "subprocess", h.delivery)
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
print(f"\n{sum(1 for _,c,_ in ok if c)}/{len(ok)} checks passed")
sys.exit(0 if all(c for _,c,_ in ok) else 1)
