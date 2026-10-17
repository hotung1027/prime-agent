
import asyncio, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
for p in ("durable_function", "continual_learning", "core"):
    sys.path.insert(0, str(ROOT / p))
import fingerprint, declutter
import learning_schedule as ls
from temporalio.client import Client
from temporalio.worker import Worker

SCRIPT = [{"changed": False, "enough": False}] * 6 + [{"changed": True, "enough": True, "turns": 9}]
calls = {"fp": 0, "distill": 0}
def fake_fp(session_id, **kw):
    r = dict(SCRIPT[min(calls["fp"], len(SCRIPT) - 1)]); calls["fp"] += 1; return r
async def fake_distill(session_id, **kw):
    calls["distill"] += 1; return {"action": "learned"}
fingerprint.fingerprint = fake_fp
fingerprint.reset_counters = lambda sid, **kw: {}
declutter.run = fake_distill

async def main():
    client = await Client.connect("localhost:7233")
    worker = Worker(client, task_queue="gate-test2",
                    workflows=[ls.FingerprintGateWorkflow],
                    activities=[ls.fingerprint_activity, ls.distill_activity])
    task = asyncio.create_task(worker.run()); await asyncio.sleep(2)
    # base 1s, backoff 2, cap 8s -> intervals 1,2,4,8,8,8 while idle
    cfg = ls.GateConfig(session_id="g2", base_interval_s=1, max_interval_s=8, backoff=2.0)
    h = await client.start_workflow(ls.FingerprintGateWorkflow.run, cfg, id="gate-g2", task_queue="gate-test2")
    seen = []
    for _ in range(50):
        await asyncio.sleep(1)
        st = await h.query(ls.FingerprintGateWorkflow.status)
        seen.append(st["interval"])
        if st["distills"] >= 1:
            break
    print("intervals observed:", seen[:12])
    print("final:", st)
    # a nudge must reset the interval to base
    await h.signal(ls.FingerprintGateWorkflow.nudge)
    await asyncio.sleep(1.5)
    st2 = await h.query(ls.FingerprintGateWorkflow.status)
    print("after nudge: interval =", st2["interval"], "nudges =", st2["nudges"])
    await h.signal(ls.FingerprintGateWorkflow.close, "done")
    await asyncio.wait_for(h.result(), timeout=20)
    task.cancel()
    return st2, seen

st2, seen = asyncio.run(main())
checks = [st2["distills"] >= 1, st2["interval"] <= 2, st2["nudges"] == 1, max(seen) <= 8]
print("PASS" if all(checks) else "FAIL", checks)
sys.exit(0 if all(checks) else 1)
