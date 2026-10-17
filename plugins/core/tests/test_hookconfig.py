
import asyncio, json, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))
import bus, hookconfig as hc

ok = []
def check(n, c, d=""):
    ok.append((n, bool(c), d)); print(f"{'PASS' if c else 'FAIL'}  {n}" + (f"   [{d}]" if not c else ""))

tmp = Path(tempfile.mkdtemp())
script = tmp / "check.sh"; script.write_text("#!/usr/bin/env bash\nexit 0\n"); script.chmod(0o755)
modfile = ROOT / "plugins" / "core" / "hooks_demo.py"
modfile.write_text("import bus\n\ndef warn(event):\n    return bus.Verdict.allow('demo')\n\ndef sched(event):\n    return bus.Verdict.allow('sched')\n")

# 1. a valid project config registers hooks of all three shapes
cfg = tmp / "hooks.json"
cfg.write_text(json.dumps({"scope": "project", "hooks": [
    {"event": "commit", "timing": "before", "delivery": "subprocess", "script": str(script), "priority": 10},
    {"event": "BeforeToolUse", "when": "^git ", "callback": "hooks_demo:warn"},
    {"event": "turn", "timing": "after", "delivery": "durable", "callback": "hooks_demo:sched"},
]}))
bus.reset()
try:
    reg = hc.load_file(cfg)
    check("a valid config registers all three hooks", len(reg) == 3, str(len(reg)))
    check("script hook is pinned by sha256", str(script.resolve()) in bus._SCRIPT_ALLOWLIST)
    check("event aliases resolved to canonical points",
          [h.name for h in bus._HANDLERS] == ["commit", "tool", "turn"], str([h.name for h in bus._HANDLERS]))
    check("filter carried from the config", bus._HANDLERS[1].when(bus.Event("tool", {"command": "git x"})))
except hc.HookConfigError as e:
    check("a valid config registers all three hooks", False, str(e))

# 2. the three refusals
for name, spec, needle in (
    ("unknown event refused", {"event": "nonsense", "callback": "hooks_demo:warn"}, "unknown event"),
    ("project file cannot register a global event", {"event": "cron:nightly", "callback": "hooks_demo:warn"}, "global-scope"),
    ("callback from an unlisted module refused", {"event": "turn", "callback": "os:system"}, "not an allowed callback source"),
    ("missing callback and script refused", {"event": "turn"}, "neither callback nor script"),
    ("missing script refused", {"event": "commit", "script": "/nope/x.sh"}, "script not found"),
):
    bus.reset(); bad = tmp / "bad.json"
    bad.write_text(json.dumps({"scope": "project", "hooks": [spec]}))
    try:
        hc.load_file(bad); check(name, False, "no error raised")
    except hc.HookConfigError as e:
        check(name, needle in str(e), str(e)[:90])

# 3. load_all: global first, then project
bus.reset()
gdir = tmp / "g"; pdir = tmp / "p"; gdir.mkdir(); pdir.mkdir()
(gdir / "g.json").write_text(json.dumps({"hooks": [{"event": "turn", "callback": "hooks_demo:warn"}]}))
(pdir / "p.json").write_text(json.dumps({"hooks": [{"event": "commit", "timing": "after", "callback": "hooks_demo:warn"}]}))
summary = hc.load_all(global_dir=gdir, project_dir=pdir)
check("load_all reports both scopes", summary["global"] == 1 and summary["project"] == 1, str(summary))

# 4. the loaded hooks actually dispatch
async def run():
    bus.reset(); calls = []
    (gdir / "g2.json").unlink(missing_ok=True)
    (gdir / "g.json").write_text(json.dumps({"hooks": [
        {"event": "BeforeToolUse", "when": "^git ", "callback": "hooks_demo:warn"}]}))
    hc.load_all(global_dir=gdir, project_dir=pdir)
    v = await bus.dispatch(bus.Event("tool", {"command": "git push --force"}))
    return v
v = asyncio.run(run())
check("a configured hook runs on dispatch", any(x.reason == "demo" for x in v), str([x.reason for x in v]))

modfile.unlink()
passed = sum(1 for _, c, _ in ok if c)
print(f"\n{passed}/{len(ok)} checks passed")
sys.exit(0 if passed == len(ok) else 1)
