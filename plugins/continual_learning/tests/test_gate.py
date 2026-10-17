
import asyncio, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
for p in ("durable_function", "continual_learning", "core"):
    sys.path.insert(0, str(ROOT / p))
import fingerprint, declutter
import learning_schedule as ls
from temporalio.client import Client
from temporalio.worker import Worker

SCRIPT = [
    {"changed": False, "enough": False},                       # 1 unchanged
    {"changed": True,  "enough": False, "turns": 2},           # 2 changed but small
    {"changed": True,  "enough": True,  "turns": 8},           # 3 distil
    {"changed": True,  "enough": True,  "turns": 8},           # 4 distil
]
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
    worker = Worker(client, task_queue="gate-test",
                    workflows=[ls.FingerprintGateWorkflow],
                    activities=[ls.fingerprint_activity, ls.distill_activity])
    task = asyncio.create_task(worker.run())
    await asyncio.sleep(2)
    cfg = ls.GateConfig(session_id="g1", interval_s=1, max_idle_passes=4)
    h = await client.start_workflow(ls.FingerprintGateWorkflow.run, cfg, id="gate-g1", task_queue="gate-test")
    res = await asyncio.wait_for(h.result(), timeout=60)
    print("result:", res, "| activity calls:", calls)
    task.cancel()
    return res, calls

res, calls = asyncio.run(main())
ok = (res["passes"] == 4 and res["distills"] == 2 and calls["fp"] == 4)
print("PASS" if ok else "FAIL", "- 2 unchanged/small skipped, 2 distilled")
sys.exit(0 if ok else 1)
