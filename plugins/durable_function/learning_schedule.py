"""Signal-gated learning schedule: distil when enough data has accumulated, never on a blind cron.

One long-lived workflow per session waits on `add_turn` / `flush` signals. It runs the declutter
activities only when the fulfillment condition holds (turns, tokens, corrections) AND the session has
been quiet for a moment. While idle it consumes no worker time and calls no model - a cron would fire
into an empty session every night.

The max-wait timeout is the safety net: a slow trickle still gets learned, a dead session never does.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy

TASK_QUEUE = "prime-pipeline"          # same worker the kernel already hosts

PRESETS = {
    # mode        min_turns  min_corrections  quiet_s  max_wait_h
    "gentle":     dict(min_turns=20, min_corrections=3, quiet_s=1800, max_wait_h=24),
    "balanced":   dict(min_turns=8,  min_corrections=1, quiet_s=600,  max_wait_h=24),
    "aggressive": dict(min_turns=2,  min_corrections=1, quiet_s=120,  max_wait_h=6),
}


@dataclass
class LearningConfig:
    session_id: str
    mode: str = "balanced"
    session_dir: str = ""
    global_: bool = False
    min_turns: int = 0
    min_corrections: int = 0
    quiet_s: int = 0
    max_wait_h: int = 0
    max_idle_h: float = 72.0      # no signal for this long -> the session is over, complete
    continue_after: int = 20      # continue-as-new after this many passes (bounded history)

    def resolved(self) -> "LearningConfig":
        preset = PRESETS.get(self.mode, PRESETS["balanced"])
        for key, value in preset.items():
            if not getattr(self, key):
                setattr(self, key, value)
        return self


@dataclass
class LearnRequest:
    session_id: str
    session_dir: str
    mode: str
    global_: bool


@activity.defn
async def learn_activity(req: LearnRequest) -> dict:
    """One declutter pass (read delta -> distill -> apply -> compact). Runs in the kernel worker."""
    import declutter
    return await declutter.run(req.session_id, session_dir=req.session_dir or None,
                               mode=req.mode, global_=req.global_)


@workflow.defn
class LearningWorkflow:
    """Accumulate signals, then distil when the data is worth it."""

    def __init__(self):
        self.turns = 0
        self.tokens = 0
        self.corrections = 0
        self.last_turn_at = 0.0
        self.flush_requested = False
        self.closed = False
        self.applied = 0
        self.passes = 0

    @workflow.signal
    async def add_turn(self, payload: dict | None = None):
        """One turn ended. payload: {"tokens": int, "kind": "turn"|"correction"|"commit"}."""
        payload = payload or {}
        self.turns += 1
        self.tokens += int(payload.get("tokens") or 0)
        if payload.get("kind") == "correction":
            self.corrections += 1
        self.last_turn_at = workflow.now().timestamp()

    @workflow.signal
    async def close(self, reason: str = "session ended"):
        self.closed = True

    @workflow.signal
    async def flush(self):
        self.flush_requested = True
        self.last_turn_at = workflow.now().timestamp()

    @workflow.query
    def status(self) -> dict:
        return {"turns": self.turns, "tokens": self.tokens, "corrections": self.corrections,
                "flush": self.flush_requested, "applied": self.applied, "closed": self.closed}

    @workflow.run
    async def run(self, cfg: LearningConfig) -> dict:
        cfg = cfg.resolved()
        while True:
            # fulfillment gate: no data -> no work, no cost. The idle horizon is what lets the
            # workflow END: without it this loop is infinite, history grows to the 50k-event limit
            # and the execution sits RUNNING forever once the worker is gone.
            try:
                await workflow.wait_condition(
                    lambda: self.closed or self.flush_requested
                            or self.turns >= cfg.min_turns
                            or self.corrections >= cfg.min_corrections,
                    timeout=timedelta(hours=cfg.max_idle_h))
            except TimeoutError:
                return {"action": "closed", "reason": "idle", "passes": self.passes}
            if self.closed:
                return {"action": "closed", "reason": "signal", "passes": self.passes}

            # Quiet period. wait_condition only re-evaluates on EVENTS (signals, activity results),
            # never on wall-clock, so a time-based predicate needs a real timer: sleep in small
            # slices until the session has been quiet for quiet_s.
            while workflow.now().timestamp() - self.last_turn_at < cfg.quiet_s and not self.closed:
                await workflow.sleep(timedelta(seconds=min(5, max(1, cfg.quiet_s))))
            if self.closed:
                return {"action": "closed", "reason": "signal", "passes": self.passes}

            result = await workflow.execute_activity(
                learn_activity,
                LearnRequest(session_id=cfg.session_id, session_dir=cfg.session_dir,
                             mode=cfg.mode, global_=cfg.global_),
                start_to_close_timeout=timedelta(minutes=20),
                retry_policy=RetryPolicy(maximum_attempts=2,
                                                  initial_interval=timedelta(seconds=20)))
            self.applied += 1
            self.passes += 1
            self.turns = 0
            self.tokens = 0
            self.corrections = 0
            self.flush_requested = False
            if self.passes >= cfg.continue_after:
                # keep history bounded on a long-lived execution
                workflow.continue_as_new(cfg)
            if cfg.mode == "gentle":          # a gentle schedule also sleeps between passes
                await workflow.sleep(timedelta(minutes=30))


async def start_for_session(client: Client, session_id: str, *, mode: str = "balanced",
                            session_dir: str = "", global_: bool = False):
    """Create (or attach to) the long-lived learning workflow for one session."""
    return await client.start_workflow(
        LearningWorkflow.run, LearningConfig(session_id=session_id, mode=mode,
                                             session_dir=session_dir, global_=global_),
        id=f"learn-{session_id}", task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)


async def signal_turn(client: Client, session_id: str, *, kind: str = "turn", tokens: int = 0):
    """Tell the session's collector that one turn ended. Cheap: no model, no worker time."""
    handle = client.get_workflow_handle(f"learn-{session_id}")
    await handle.signal(LearningWorkflow.add_turn, {"tokens": int(tokens), "kind": kind})
    return True


# --------------------------------------------------------------------------------------------------
# The fingerprint gate: a scheduled loop, keyed by inputs. No signal required, so nobody has to
# remember to announce a turn - and an unchanged tick never reaches a model.

@dataclass
class GateConfig:
    session_id: str
    interval_s: int = 300          # wake every 5 minutes
    session_dir: str = ""
    global_: bool = False
    min_turns: int = 8
    min_corrections: int = 1
    min_tokens: int = 0
    max_idle_passes: int = 0       # 0 = run until closed


@dataclass
class GateRequest:
    session_id: str
    session_dir: str
    min_turns: int
    min_corrections: int
    min_tokens: int


@activity.defn
async def fingerprint_activity(req: GateRequest) -> dict:
    import fingerprint
    return fingerprint.fingerprint(req.session_id, session_dir=req.session_dir or None,
                                   min_turns=req.min_turns, min_corrections=req.min_corrections,
                                   min_tokens=req.min_tokens)


@activity.defn
async def distill_activity(req: GateRequest) -> dict:
    import declutter
    import fingerprint
    result = await declutter.run(req.session_id, session_dir=req.session_dir or None)
    fingerprint.reset_counters(req.session_id)
    return result


@workflow.defn
class FingerprintGateWorkflow:
    """Wake on a timer, fingerprint the inputs, distil only when they changed and the data is enough."""

    def __init__(self):
        self.passes = 0
        self.distills = 0
        self.skipped_unchanged = 0
        self.skipped_small = 0
        self.closed = False
        self.last = None

    @workflow.signal
    async def close(self, reason: str = ""):
        self.closed = True

    @workflow.query
    def status(self) -> dict:
        return {"passes": self.passes, "distills": self.distills,
                "skipped_unchanged": self.skipped_unchanged, "skipped_small": self.skipped_small,
                "closed": self.closed, "last": self.last}

    @workflow.run
    async def run(self, cfg: GateConfig) -> dict:
        req = GateRequest(session_id=cfg.session_id, session_dir=cfg.session_dir,
                          min_turns=cfg.min_turns, min_corrections=cfg.min_corrections,
                          min_tokens=cfg.min_tokens)
        while not self.closed:
            await workflow.sleep(timedelta(seconds=max(1, cfg.interval_s)))
            if self.closed:
                break
            fp = await workflow.execute_activity(
                fingerprint_activity, req, start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=2))
            self.passes += 1
            self.last = fp.get("conv_hash")
            if not fp.get("changed"):
                self.skipped_unchanged += 1
                continue
            if not fp.get("enough"):
                self.skipped_small += 1
                continue
            await workflow.execute_activity(
                distill_activity, req, start_to_close_timeout=timedelta(minutes=20),
                retry_policy=RetryPolicy(maximum_attempts=2))
            self.distills += 1
            if cfg.max_idle_passes and self.passes >= cfg.max_idle_passes:
                break
        return {"passes": self.passes, "distills": self.distills, "reason": "closed" if self.closed else "budget"}
