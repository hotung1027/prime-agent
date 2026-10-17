"""Temporal-owned staged delivery engine.

Why: the hand-rolled engine had no durable state. Today it (a) let two implementers write the same
file, (b) could not be resumed after the orchestrator lost track, and (c) needed a human to work out
which stage was in flight. Temporal fixes all three: a workflow is a durable state machine, each
stage is one activity attempt, retries are explicit, and the run is visible in the Temporal UI.

The WORKER RUNS INSIDE THE prime-agent KERNEL, because activities spawn child agents through the
`rlm` module, which only exists there. Start it with `await start_worker()` (returns a task).

    from temporal_backend import DeliveryPipelineWorkflow, PipelineRequest, start_worker
    worker_task = await start_worker()
    handle = await start_run(brief, run_dir=...)      # see start_run below
"""
import json
import os
from dataclasses import dataclass, field
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.client import Client
from temporalio.worker import Worker

TASK_QUEUE = "prime-pipeline"
ADDR = "localhost:7233"

# roles that a stage/round may run; each one is one activity attempt
ROLE_SEQUENCE = ("researcher", "planner", "implementer", "reviewer", "verifier")


@dataclass
class RoleRequest:
    """Everything an activity needs. Activities must not close over workflow-local state."""
    role_id: str
    run_dir: str
    values: dict = field(default_factory=dict)
    task_kind: str = "default"
    task_brief: str = ""
    model: str | None = None
    thinking: str | None = None
    route: bool = False
    exclude: list = field(default_factory=list)


@dataclass
class PipelineRequest:
    brief: str
    run_dir: str
    stage: str = "dev"
    task_kind: str = "default"
    max_rounds: int = 3
    gates: list = field(default_factory=list)
    route: bool = False
    # Resume support: `from_stage="review"` (or "implement") reuses the artifacts already in run_dir
    # instead of re-running research/planning. This is the reason the pipeline moved to Temporal: a
    # stalled run used to need a human to work out which stage was in flight.
    from_stage: str = "research"          # research | implement | review
    spec_path: str = ""
    impl: dict = field(default_factory=dict)


@activity.defn
async def run_role(req: RoleRequest) -> dict:
    """One stage = one activity attempt. Retried by Temporal, never run twice concurrently.

    The activity HEARTBEATS while it waits: role runs take 10-45 min and Temporal's heartbeat timeout
    cancels a silent activity, whose retry then spawns a SECOND child for the same stage (that is how
    two implementers ended up writing the same source file). On cancellation the child is taken down too.
    """
    import asyncio
    import core
    import engine

    spawned: dict = {}

    def on_spawn(handle):
        spawned["name"] = handle.name

    task = asyncio.create_task(engine.run_role_stage(
        req.role_id, run_dir=req.run_dir, values=req.values, task_kind=req.task_kind,
        model=req.model, thinking=req.thinking, route=req.route, task_brief=req.task_brief,
        exclude=req.exclude, on_spawn=on_spawn))
    try:
        while not task.done():
            activity.heartbeat(f"{req.role_id}: running")
            await asyncio.sleep(20)
        return await task
    except asyncio.CancelledError:
        task.cancel()
        if spawned.get("name"):
            await core.stop_child(spawned["name"])
        raise


@activity.defn
async def run_command(cmd: str, cwd: str = ".", timeout_s: int = 1800) -> dict:
    """Run a shell command the factory needs (tests, exporters, acceptance checks)."""
    import asyncio
    activity.heartbeat(cmd[:120])
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        return {"exit_code": -1, "output": f"timeout after {timeout_s}s"}
    text = (out or b"").decode("utf-8", "replace")
    return {"exit_code": proc.returncode, "output": text[-4000:]}


@workflow.defn
class DeliveryPipelineWorkflow:
    """research -> plan -> (implement -> review -> verify -> gates) x max_rounds."""

    @workflow.run
    async def run(self, req: PipelineRequest) -> dict:
        def role(r, **values):
            return workflow.execute_activity(
                run_role, RoleRequest(role_id=r, run_dir=req.run_dir, values=values,
                                      task_kind=req.task_kind, task_brief=req.brief, route=req.route),
                start_to_close_timeout=timedelta(minutes=45),
                heartbeat_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=3, initial_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0))

        if req.from_stage == "research":
            research = await role("researcher", task=req.brief)
            spec = await role("planner", research=research["parsed"])
            spec_path = (spec["parsed"] or {}).get("spec_path", f"{req.run_dir}/spec.md")
        else:
            # resume: the spec already exists on disk (and possibly an implementation to review)
            spec_path = req.spec_path or f"{req.run_dir}/spec.md"

        if req.from_stage == "gates":
            # gates only: the review/verify stages already passed and are recorded on disk
            gates = {}
            ok = True
            for gate in req.gates:
                g = await role(gate, spec_path=spec_path, impl=req.impl)
                gates[gate] = (g["parsed"] or {}).get(gate, g["parsed"])
                if not (gates[gate] or {}).get("passed"):
                    ok = False
                    break
            return {"ok": ok, "gates": gates, "spec": spec_path, "mode": "gates-only"}

        verdict = check = gates = None
        impl_used = None
        impl_body = dict(req.impl) if req.impl else None      # seeded => round 1 skips the implementer
        for rounds in range(1, req.max_rounds + 1):
            if impl_body is None or rounds > 1:
                impl = await role("implementer", spec_path=spec_path, verdict=verdict, check=check,
                                  gates=gates)
                impl_body = (impl["parsed"] or {}).get("impl", impl["parsed"])
                impl_used = impl["model"]
            verdict = await role("reviewer", spec_path=spec_path, impl=impl_body)
            if not ((verdict["parsed"] or {}).get("verdict", {}) or {}).get("approved"):
                continue
            check = await role("verifier", spec_path=spec_path, impl=impl_body)
            if not ((check["parsed"] or {}).get("check", {}) or {}).get("passed"):
                continue
            gates = {}
            ok = True
            for gate in req.gates:
                g = await role(gate, spec_path=spec_path, impl=impl_body)
                gates[gate] = (g["parsed"] or {}).get(gate, g["parsed"])
                if not (gates[gate] or {}).get("passed"):
                    ok = False
                    break
            if ok:
                return {"ok": True, "rounds": rounds, "spec": spec_path, "implementer_model": impl_used}

        return {"ok": False, "reason": "max rounds reached", "spec": spec_path,
                "implementer_model": impl_used, "stage": req.stage}


@workflow.defn
class SmokeWorkflow:
    """Two commands back to back. Proves worker + activities + history with no model spend."""

    @workflow.run
    async def run(self, cwd: str) -> dict:
        a = await workflow.execute_activity(run_command, args=["echo activity-one", cwd],
                                            start_to_close_timeout=timedelta(minutes=2))
        b = await workflow.execute_activity(run_command, args=["sh -c 'echo activity-two; exit 0'", cwd],
                                            start_to_close_timeout=timedelta(minutes=2))
        return {"first": a["output"].strip(), "second": b["output"].strip()}




async def connect() -> Client:
    return await Client.connect(ADDR)


async def start_worker(task_queue: str = TASK_QUEUE, client: Client | None = None):
    """Create the worker and run it as a background task on the CURRENT loop (the kernel's)."""
    import asyncio
    client = client or await connect()
    worker = Worker(client, task_queue=task_queue, workflows=WORKFLOWS, activities=ACTIVITIES)
    return client, worker, asyncio.create_task(worker.run())


async def start_run(brief: str, run_dir: str, *, stage: str = "dev", task_kind: str = "default",
                         max_rounds: int = 3, gates=None, route: bool = False, workflow_id: str | None = None,
                         client: Client | None = None):
    """Write task.md, then start the workflow. Returns (handle, run_dir)."""
    import engine
    client = client or await connect()
    run_dir = os.path.abspath(run_dir)
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "task.md"), "w") as fh:
        fh.write(brief)
    if gates is None:
        gates = engine._load_roles()[1].get(stage, [])
    req = PipelineRequest(brief=brief, run_dir=run_dir, stage=stage, task_kind=task_kind,
                          max_rounds=max_rounds, gates=list(gates), route=route)
    handle = await client.start_workflow(
        DeliveryPipelineWorkflow.run, req,
        id=workflow_id or f"pipeline-{os.path.basename(run_dir)}",
        task_queue=TASK_QUEUE)
    return handle, run_dir


@activity.defn
async def probe_child(prompt: str, model: str) -> dict:
    """Spawn one trivial child through the SAME path the real roles use (_run_role -> rlm.spawn).

    This is the integration check that matters: an activity must be able to spawn a child agent even
    though the worker runs as a background task on the kernel's event loop.
    """
    import engine
    name = "probe-" + os.urandom(2).hex()
    handle, parsed, used = await core.run_child(name, prompt, "probe", models=[model])
    return {"parsed": parsed, "model": used, "child": handle.name}


@workflow.defn
class ProbeWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        return await workflow.execute_activity(
            probe_child, args=[payload["prompt"], payload["model"]],
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=2))



@workflow.defn
class RustProbeWorkflow:
    """Cross-language routing check: this workflow executes on the Python worker while its activity is
    executed by a RUST worker on the `rust-probe` task queue."""

    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            "greet", args=[name], task_queue="rust-probe",
            start_to_close_timeout=timedelta(seconds=60))


# registries last: every workflow/activity must exist before the lists are built



@workflow.defn
class RustToolsWorkflow:
    """Drives the production Rust worker: inspect an exported trajectory (pure Rust), then run the
    Julia test suite through it. Proves the typed payload contract both ways."""

    @workflow.run
    async def run(self, req: dict) -> dict:
        traj = await workflow.execute_activity(
            "inspect_trajectory", args=[req["traj_path"]], task_queue=RUST_TOOLS_QUEUE,
            start_to_close_timeout=timedelta(seconds=60))
        tests = await workflow.execute_activity(
            "run_tests", args=[req["tests_request"]], task_queue=RUST_TOOLS_QUEUE,
            start_to_close_timeout=timedelta(minutes=30))
        return {"trajectory": traj, "tests": tests}


@workflow.defn
class RustCommandWorkflow:
    """Run one mechanical command through the Rust worker (e.g. an acceptance command from a spec)."""

    @workflow.run
    async def run(self, req: dict) -> dict:
        return await workflow.execute_activity(
            "run_command", args=[req], task_queue=RUST_TOOLS_QUEUE,
            start_to_close_timeout=timedelta(minutes=30))

RUST_TOOLS_QUEUE = "rust-tools"

# registries LAST: every workflow/activity must exist before these lists are built
WORKFLOWS = [DeliveryPipelineWorkflow, SmokeWorkflow, ProbeWorkflow, RustProbeWorkflow, RustToolsWorkflow, RustCommandWorkflow]
ACTIVITIES = [run_role, run_command, probe_child]
