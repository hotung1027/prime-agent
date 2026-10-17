
import asyncio, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import bus
ok = []
def check(n, c, d=""):
    ok.append((n, bool(c), d)); print(f"{'PASS' if c else 'FAIL'}  {n}" + (f"   [{d}]" if not c else ""))

async def main():
    # 1. pub/sub: add(event, callback=..., when=regex)
    bus.reset(); hits = []
    def lint(e): hits.append(e.text()); return bus.Verdict.allow()
    bus.add("tool", callback=lint, timing="before", when=r"^git ")
    await bus.dispatch(bus.Event("tool", {"tool": "bash", "command": "git commit -m x"}))
    await bus.dispatch(bus.Event("tool", {"tool": "bash", "command": "rm -rf /tmp/x"}))
    check("regex filter matches the command", hits == ["git commit -m x"], str(hits))

    # 2. dict filter: {field: regex}
    bus.reset(); hits = []
    bus.add("tool", callback=lambda e: hits.append(e.text()) or bus.Verdict.allow(),
            when={"tool": r"^bash$"})
    await bus.dispatch(bus.Event("tool", {"tool": "bash", "command": "ls"}))
    await bus.dispatch(bus.Event("tool", {"tool": "edit", "path": "x.py"}))
    check("dict filter selects by field", hits == ["ls"], str(hits))

    # 3. filter mandatory on high-frequency points
    try:
        bus.add("tool", callback=lint); check("missing filter refused", False)
    except ValueError as e: check("missing filter refused", "needs a filter" in str(e))

    # 4. decorator form still works
    bus.reset()
    @bus.on("commit", when=lambda e: e.attrs.get("staged"))
    def gate(e): return bus.Verdict.block("too big")
    v = await bus.dispatch(bus.Event("commit", {"staged": True}))
    check("on() decorator still registers", v and v[0].verdict == "block", str([x.verdict for x in v]))

    # 5. Event.text falls back to JSON
    e = bus.Event("commit", {"sha": "abc", "loc": 12})
    check("text() falls back to canonical json", '"sha": "abc"' in e.text(), e.text())

    # 6. timing aliases still resolve through add
    bus.reset(); seen = []
    bus.add("pre_tool", callback=lambda e: seen.append(1) or bus.Verdict.allow(), when=r".")
    await bus.dispatch(bus.Event("tool", {"command": "x"}))
    check("pre_tool via add", seen == [1], str(seen))

asyncio.run(main())
failed = [c for c in ok if not c[1]]
print(f"\n{len(ok)-len(failed)}/{len(ok)} checks passed")
sys.exit(1 if failed else 0)
