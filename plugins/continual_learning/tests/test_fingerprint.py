
import json, os, sys, tempfile, time, tracemalloc
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import fingerprint as fpg

results = []
def check(name, ok, detail=""):
    results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if not ok else ""))

tmp = Path(tempfile.mkdtemp())
os.environ["PRIME_SESSION_DIR"] = str(tmp)
os.environ["PRIME_LEARN_CURSOR"] = str(tmp / ".fp.json")
fpg.STATE_PATH = tmp / ".fp.json"; fpg.SESSION_DIR = tmp
SID = "big"
path = tmp / f"{SID}.jsonl"

# ~4 MB of realistic session lines
line = {"type": "message", "message": {"role": "assistant",
        "content": [{"type": "text", "text": "x" * 400}]}}
with open(path, "w") as fh:
    for i in range(10000):
        fh.write(json.dumps(line) + "\n")
size_mb = path.stat().st_size / 1e6
print(f"session: {size_mb:.1f} MB")

# 1. cold fingerprint: time + peak memory
tracemalloc.start()
t0 = time.perf_counter(); r1 = fpg.fingerprint(SID); cold = (time.perf_counter() - t0) * 1000
_, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
print(f"cold   : {cold:7.1f} ms  peak {peak/1e6:.2f} MB  changed={r1['changed']} turns={r1['turns']}")
check("cold pass hashes the file", r1["changed"] and r1["turns"] == 10000, str(r1.get("turns")))
# 2. warm: unchanged -> stat only
t0 = time.perf_counter(); r2 = fpg.fingerprint(SID); warm = (time.perf_counter() - t0) * 1000
print(f"warm   : {warm:7.3f} ms  changed={r2['changed']} reason={r2.get('reason')}")
check("unchanged tick is a no-op", r2["changed"] is False and r2["reason"] == "unchanged", str(r2))
check("unchanged tick is cheap (< 2 ms)", warm < 2.0, f"{warm:.3f} ms")

# 3. append three turns, one a correction
with open(path, "a") as fh:
    fh.write(json.dumps({"type": "message", "message": {"role": "user",
             "content": [{"type": "text", "text": "no - one commit, one responsibility"}]}}) + "\n")
    for _ in range(2):
        fh.write(json.dumps({"type": "message", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "done"}]}}) + "\n")
import gc; gc.collect()          # the 20 MB memory test above leaves garbage; time the call, not the GC
t0 = time.perf_counter(); r3 = fpg.fingerprint(SID); inc = (time.perf_counter() - t0) * 1000
print(f"delta  : {inc:7.3f} ms  turns={r3['turns']} corrections={r3['corrections']} offset={r3['offset']} size={path.stat().st_size}")
check("delta counts only the new turns", r3["turns"] == 3 and r3["corrections"] == 1, str(r3))
check("a small delta is cheap in absolute terms (< 5 ms)", inc < 5.0, f"{inc:.3f} ms")
check("thresholds: enough after a correction", r3["enough"] is True, str(r3.get("totals")))

# 4. AST hash: formatting/comments do not count, structure does
a = "def f(x):\n    return x + 1\n"
b = "def f(x):\n    # a comment\n    return x +  1\n"
c = "def f(x):\n    return x + 2\n"
check("ast_hash ignores formatting and comments", fpg.ast_hash(a) == fpg.ast_hash(b))
check("ast_hash detects a real change", fpg.ast_hash(a) != fpg.ast_hash(c))

# 5. reset keeps the offset, zeroes the accumulation
fpg.reset_counters(SID)
r4 = fpg.fingerprint(SID)
check("reset zeroes counters and keeps the offset", r4["changed"] is False, str(r4))

# boundedness, not absolute size: a 4x larger file must not mean a 4x larger peak
big = tmp / "bigger.jsonl"
with open(big, "w") as fh:
    fh.write(path.read_text())
    fh.write(path.read_text()); fh.write(path.read_text()); fh.write(path.read_text())
tracemalloc.start()
r_big = fpg.fingerprint("bigger")
_, peak_big = tracemalloc.get_traced_memory(); tracemalloc.stop()
print(f"20 MB : peak {peak_big/1e6:.2f} MB")
check("peak memory does not scale with the file", peak_big < peak * 2.0,
      f"{peak_big/1e6:.2f} MB vs {peak/1e6:.2f} MB")


print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
