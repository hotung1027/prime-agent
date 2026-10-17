
"""Let the decision model pick the model - and sync vs batch - per role, from a pre-selected pool.

The pool is curated (available + benchmarked + policy-checked); the decision layer only chooses
WITHIN it, from the task description. A choice below the confidence bar escalates to the caller,
which then falls back to the deterministic tier default from engine.json.

    import routing
    pick = await routing.route("implementer", "Refactor the parser and add tests", pool=[...])
    # {"model": "...", "mode": "sync", "confidence": 0.93, "escalate": False, "reason": ..., "answers": {...}}

`mode` is only offered when allow_batch=True: batch requests are single-turn and non-streaming
(measured: a 1-request job was still queued after 13 minutes, 24h window), so they can never host an
agentic role that must call tools. Use allow_batch=True only for one-shot bulk work.
"""

from __future__ import annotations

import decision
import core

# role -> [(model_id, what it is good at)], the pre-selected pool the decision layer may choose from
POOL = {
    "implementer": [
        ("openrouter/deepseek/deepseek-v4.1-flash", "fast, ~70 tok/s, cheapest; best for bulk or mechanical edits"),
        ("openrouter/xiaomi/mimo-v2.6-pro", "stronger reasoning/quality (SciCode 60.9), ~33 tok/s, 1.25x the token price"),
        ("openrouter/z-ai/glm-5.3-flash", "quality alternative, 91.2 GPQA, ~30 tok/s, cheapest of the three"),
    ],
    "planner": [
        ("openrouter/xiaomi/mimo-v2.6-pro", "best planning score (HLE 49.4)"),
        ("openrouter/z-ai/glm-5.3-flash", "cheaper planning, lower HLE (39.9)"),
        ("openrouter/deepseek/deepseek-v4.1-flash", "fastest, weakest planning (HLE 39.2)"),
    ],
    "reviewer": [
        ("openrouter/z-ai/glm-5.3-flash", "different family from the deepseek implementer; cheapest"),
        ("openrouter/xiaomi/mimo-v2.6-pro", "strongest SciCode in the pool (60.9)"),
    ],
    "verifier": [
        ("openrouter/xiaomi/mimo-v2.6-pro", "agentic execution score 34.8 (Terminal-Bench)"),
        ("openrouter/z-ai/glm-5.3-flash", "cheaper, 32.8 Terminal-Bench"),
        ("openrouter/deepseek/deepseek-v4.1-flash", "fastest run, 26.8 Terminal-Bench"),
    ],
    "researcher": [
        ("openrouter/xiaomi/mimo-v2.6-pro", "best scientific score (CritPt 26.6)"),
        ("openrouter/deepseek/deepseek-v4.1-flash", "fastest for web lookups and repo reading"),
        ("openrouter/z-ai/glm-5.3-flash", "cheapest, weaker on CritPt (15.4)"),
    ],
}
MODE_CHOICES = [
    ("sync", "agentic: many turns, tool calls, or the result is needed soon"),
    ("batch", "single-turn, no tools, and the result may arrive hours later (fixed 24h window)"),
]

# The completion window is HARD-CODED to 24h: the API rejects any other value
# ("completion_window: Invalid input: expected \"24h\""), so a batch result is at best hours away.
# A batch task may only start when EVERY condition below holds. Anything else -> sync.
BATCH_PRECONDITIONS = (
    "single-turn: one prompt, no tool calls, no follow-up turns",
    "nothing is blocked waiting for it (not in a pipeline's critical path)",
    "the consumer tolerates a result arriving hours later (24h window, not configurable)",
    "many independent items share one job (a 1-item job sat queued >17 min)",
    "the 50-62% discount is worth the delay",
)


def batch_gate(*, single_turn=False, blocking=True, latency_ok=False, items=1, savings_matter=False):
    """Strict precondition check. Returns (allowed, [failed conditions]). All five must hold."""
    checks = {
        "single_turn": bool(single_turn),
        "not_blocking": not bool(blocking),
        "latency_ok": bool(latency_ok),
        "many_items": int(items) >= 8,
        "savings_matter": bool(savings_matter),
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        return False, [f"{name} ({BATCH_PRECONDITIONS[i]})" for i, name in enumerate(checks) if name in failed]
    return True, []


async def route(role, task, *, pool=None, exclude=(), threshold=0.9, require_confident=False,
                allow_batch=False, batch_context=None, state=None, cfg=None):
    """Pick a model from the pool.

    `require_confident=False` (the default for ROUTING): take jev's top pick - the option with the
    best probability and its confidence - and do not escalate to the tier default. The user's rule:
    the decision layer's best-probability option is good enough to act on here, because the pool was
    already curated and every candidate is acceptable. Escalation is reserved for the user-facing
    ask_or_decide path.
    `require_confident=True` restores the 0.9 bar for callers that want it."""
    candidates = pool or POOL.get(role) or POOL["implementer"]
    if exclude:
        kept = [(mid, why) for mid, why in candidates if mid not in set(exclude)]
        if kept:                      # never empty the pool: an empty choice is unusable
            candidates = kept
    questions = {
        "model": decision.choice(f"Which model should run the {role} role for this task?",
                                 [(mid, why) for mid, why in candidates]),
    }
    # The strict gate runs BEFORE the decision model is asked: an unqualified batch task is never
    # offered, so jev cannot choose it by mistake.
    batch_ok, batch_failed = (False, ["allow_batch not requested"])
    if allow_batch:
        batch_ok, batch_failed = batch_gate(**(batch_context or {}))
    mode_locked = "sync"
    if batch_ok:
        questions["mode"] = decision.choice(
            "Synchronous agentic call or asynchronous batch? Prefer sync unless the work is clearly "
            "offline: a batch result cannot arrive for hours (fixed 24h window) and a small job just "
            "queues.",
            MODE_CHOICES)
    state_payload = {"role": role, "task": task[:1500],
                     "candidates": [{"model": mid, "strength": why} for mid, why in candidates]}
    reply = await decision.ask(state_payload, questions, cfg=cfg)
    answers = reply["answers"]
    model_ans = answers.get("model") or {}
    mode_ans = answers.get("mode") or {}
    model = str(model_ans.get("choice") or "")
    valid = {mid for mid, _ in candidates}
    confidence = float(model_ans.get("confidence") or 0.0)
    option_conf = float((model_ans.get("probabilities") or {}).get(model, 0.0))
    escalate = decision._escalates(option_conf, confidence, threshold,
                                   (cfg or core.load_settings()).get("decision_rule"))
    if model not in valid:
        escalate = True
    elif not require_confident:
        escalate = False          # routing trusts jev's top pick (pool already curated)
    mode = str(mode_ans.get("choice") or "sync") if batch_ok else "sync"
    # fail-safe: an escalated mode question means sync, never batch
    if batch_ok and escalate:
        mode = "sync"
    return {"role": role, "model": model, "mode": mode, "confidence": confidence,
            "batch_allowed": batch_ok, "batch_blocked_by": batch_failed,
            "option_confidence": option_conf, "escalate": escalate, "pool": sorted(valid),
            "probabilities": model_ans.get("probabilities"), "reason": model_ans.get("reason", ""),
            "cost_usd": reply.get("cost_usd"), "api": reply.get("api")}


async def route_or_default(role, task, *, default, allow_batch=False, **kw):
    """Route, and fall back to the deterministic default when the decision escalates."""
    pick = await route(role, task, allow_batch=allow_batch, **kw)
    if pick["escalate"] or not pick["model"]:
        pick["used"] = default
        pick["fell_back"] = True
    else:
        pick["used"] = pick["model"]
        pick["fell_back"] = False
    return pick
