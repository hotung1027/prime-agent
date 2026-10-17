
"""Staged multi-agent delivery pipeline for Prime Agent.

Roles (child agents): researcher, planner, implementer/debugger, reviewer, verifier,
documentator, plus the stage gates security, profiling, infrastructure and cicd.
Stages (dev/test/staging/prod) select which gates run. Hooks fire shell commands or
MCP tool calls on events such as `implemented`, `merged` or `file_changed`.

Usage (Python kernel):
    import os
    exec(open(os.path.expanduser("~/.prime/agent/pipelines/engine.py")).read())
    result = await run_stages("<task brief>", stage="staging")

Config: ~/.prime/agent/pipelines/engine.json (created on first run) holds the
`hooks` map and the `models` map; edit it to wire git commits, file-change hooks or
Linear updates.
"""

from __future__ import annotations

import asyncio
import json
import sys
import os
import re
import time
from pathlib import Path

import rlm
from rlm import bash

import core          # bottom layer: model chains + child spawn

try:
    import tracing
except Exception:  # tracing is optional; never break a run over it
    tracing = None

HERE = (Path(__file__).resolve().parent if "__file__" in globals()
        else Path(os.path.expanduser("~/.prime/agent/pipelines")))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
CONFIG_PATH = HERE / "engine.json"
WORK_DIR = Path(".prime-factory")
POLL_S = 2.0
ROLE_TIMEOUT_S = 900.0
DEFAULT_ROUNDS = 3

DEFAULT_CONFIG = {
    "hooks": {},
    "models": {"default": None, "reviewer": None, "gates": None},
    "docs_stages": ["staging", "prod"],
    "_hook_events": [
        "run_start", "researched", "planned", "implemented", "file_changed",
        "reviewed", "verified", "gate", "documented", "merged", "run_end", "failure",
    ],
    "_hook_examples": {
        "implemented": [
            {"type": "shell", "command": "git add -A && git commit -m 'pipeline: {task_title}' || true"}
        ],
        "merged": [
            {"type": "shell", "command": "git push"},
            {"type": "mcp", "server": "linear", "tool": "create_comment",
             "arguments": {"issueId": "{linear_id}", "body": "Delivered: {task_title}"}},
        ],
    },
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
    cfg = json.loads(CONFIG_PATH.read_text())
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["hooks"] = cfg.get("hooks") or {}
    merged["models"] = {**DEFAULT_CONFIG["models"], **(cfg.get("models") or {})}
    return merged


def _load_roles():
    ns: dict = {}
    exec(compile((HERE / "roles.py").read_text(), str(HERE / "roles.py"), "exec"), ns)
    return ns["ROLES"], ns["STAGES"]


# ---------------------------------------------------------------- answers



def _guess_kind(brief, explicit=None):
    """Route the researcher: web facts vs scientific literature vs ordinary code work."""
    if explicit:
        return explicit
    low = brief.lower()
    if any(w in low for w in ("web", "search", "url", "docs page", "datasheet", "issue tracker", "mcp")):
        return "websearch"
    if any(w in low for w in ("paper", "literature", "proof", "experiment", "benchmark", "dataset", "scientific", "arxiv")):
        return "scientific"
    return "default"


# ---------------------------------------------------------------- hooks

def _render(value, ctx):
    if isinstance(value, str):
        for key, item in ctx.items():
            value = value.replace("{" + key + "}", item if isinstance(item, str) else json.dumps(item))
        return value
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


async def fire(event, ctx, cfg, log):
    results = []
    for action in cfg["hooks"].get(event, []):
        kind = action.get("type")
        try:
            if kind == "shell":
                command = _render(action["command"], ctx)
                handle = bash(command)
                await handle
                results.append({"event": event, "type": "shell", "command": command,
                                "exit": handle.exit_code, "output": handle.output()[-500:]})
            elif kind == "mcp":
                args = _render(action.get("arguments") or {}, ctx)
                reply = await rlm.mcp.call_tool(action["server"], action["tool"], args)
                results.append({"event": event, "type": "mcp", "server": action["server"],
                                "tool": action["tool"], "ok": True, "reply": str(reply)[:300]})
            elif kind == "agent":
                prompt = _render(action["prompt"], ctx)
                handle, _, _used = await core.run_child(f"hook-{event}-{os.urandom(2).hex()}", prompt,
                                                   action.get("expect", "ok"), timeout=600)
                results.append({"event": event, "type": "agent", "ok": True})
            else:
                results.append({"event": event, "type": kind, "error": "unknown hook type"})
        except Exception as exc:  # a failed hook is reported, never fatal
            results.append({"event": event, "type": kind, "error": f"{type(exc).__name__}: {exc}"})
    log.extend(results)
    return results


# ---------------------------------------------------------------- pipeline

async def run_stages(task_brief, *, stage="dev", work_dir=WORK_DIR, max_rounds=DEFAULT_ROUNDS,
                       keep_children=False, cfg=None, model=None, reviewer_model=None,
                       gate_model=None, linear_id=None, task_kind=None, route_models=False):
    """Run the staged engine. `stage` is one of dev, test, staging, prod."""
    cfg = cfg or load_config()
    task_kind = _guess_kind(task_brief, task_kind)
    roles, stages = _load_roles()
    if stage not in stages:
        raise ValueError(f"unknown stage {stage!r}; expected one of {sorted(stages)}")

    wd = Path(work_dir)
    wd.mkdir(parents=True, exist_ok=True)
    run_tag = os.urandom(3).hex()
    run_dir = (wd / run_tag).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.md").write_text(task_brief)
    children, log, ctx = [], [], {
        "stage": stage, "task_title": task_brief.strip().splitlines()[0][:60],
        "branch": "", "linear_id": linear_id or "", "head": "",
    }

    async def role(role_id, *, model=None, thinking=None, **values):
        prompt = roles[role_id][0]
        prompt = prompt.replace(".prime-factory/", f"{run_dir}/")
        for key, value in values.items():
            rendered = value if isinstance(value, str) else json.dumps(value)
            prompt = prompt.replace("{" + key + "}", rendered)
        # a unique suffix per spawn: a restart (or a still-alive child from a previous run)
        # must never make the supervisor reject the name
        name = f"pl-{role_id}-{run_tag}-{len(children)}-{os.urandom(2).hex()}"[:64]
        chain = [model] if model else core.models_for(role_id, kind=task_kind)
        if route_models and model is None:
            try:
                import routing
                # the reviewer must never run the same model as the implementer that wrote the code
                excl = set()
                if role_id == "reviewer":
                    impl = next((e for e in reversed(log) if isinstance(e, dict)
                                 and e.get("event") == "role_model" and e.get("role") == "implementer"), None)
                    if impl:
                        excl = {impl["model"]}
                pick = await routing.route_or_default(role_id, task_brief, default=chain[0], exclude=excl)
                log.append({"event": "role_route", "role": role_id, "chosen": pick["model"],
                            "used": pick["used"], "confidence": pick["confidence"],
                            "option_confidence": pick["option_confidence"],
                            "escalated": pick["escalate"], "mode": pick["mode"],
                            "cost_usd": pick["cost_usd"]})
                if pick["used"] and pick["used"] in chain:
                    chain = [pick["used"]] + [m for m in chain if m != pick["used"]]
                elif pick["used"]:
                    chain = [pick["used"]] + chain
            except Exception as exc:
                log.append({"event": "role_route", "role": role_id, "error": f"{type(exc).__name__}: {exc}"})
        span = tracing.span(f"role:{role_id}", stage=stage, models=chain, run=run_tag) if tracing else None
        started = time.monotonic()
        if span:
            span.__enter__()
        try:
            handle, parsed, used = await core.run_child(name, prompt, roles[role_id][1],
                                                   models=chain, thinking=thinking)
        except Exception as exc:
            if span:
                span.attr(error=f"{type(exc).__name__}: {exc}", duration_ms=round((time.monotonic()-started)*1000, 1))
                span.__exit__(type(exc), exc, None)
            if tracing:
                tracing.event("role-failed", role=role_id, error=f"{type(exc).__name__}: {exc}"[:200])
            raise
        children.append(handle)
        if span:
            span.attr(model=used, handle=handle.name, duration_ms=round((time.monotonic()-started)*1000, 1))
            span.__exit__(None, None, None)
        log.append({"event": "role_model", "role": role_id, "model": used})
        return parsed

    if tracing:
        # v4: correlation ids must exist BEFORE any observation-producing call. The session is the run
        # tag, so every role span groups and prices under one session; start_trace also records the
        # overall request on the root observation (there is no trace-level input in v4).
        tracing.start_trace(
            f"pipeline:{stage}", session_id=run_tag,
            user_id=os.environ.get("PRIME_AGENT_USER") or None,
            input={"task_brief": task_brief, "stage": stage, "kind": task_kind, "run_dir": str(run_dir)},
            tags=["pipeline", stage, task_kind],
            version=os.environ.get("PRIME_AGENT_VERSION") or None,
            metadata={"run_dir": str(run_dir)})
        tracing.event("run-start", stage=stage, brief=task_brief[:160], kind=task_kind)
    try:
        # Treat the model catalog as a cache: pull newly published models before spawning roles.
        try:
            import model_cache_refresh
            summary = model_cache_refresh.refresh_models(verbose=False)
            log.append({"event": "model_cache", **{k: v for k, v in summary.items() if k != "path"}})
        except Exception as exc:
            log.append({"event": "model_cache", "ok": False, "error": str(exc)})

        await fire("run_start", ctx, cfg, log)

        research = await role("researcher", task=task_brief)
        await fire("researched", ctx, cfg, log)

        spec = await role("planner", research=research)
        spec_path = spec.get("spec_path", str(run_dir / "spec.md"))
        await fire("planned", ctx, cfg, log)

        impl = verdict = check = None
        gate_results = {}
        approved = passed = False
        for rounds in range(1, max_rounds + 1):
            impl = await role("implementer", model=model, spec_path=spec_path, verdict=verdict,
                              check=check, gates=gate_results or None)
            ctx["head"] = impl.get("impl", {}).get("head") or ctx["head"]
            ctx["branch"] = impl.get("impl", {}).get("branch") or ctx["branch"]
            await fire("implemented", ctx, cfg, log)
            await fire("file_changed", ctx, cfg, log)

            verdict = await role("reviewer", model=reviewer_model or model, thinking="high",
                                 spec_path=spec_path, impl=impl.get("impl", impl))
            approved = bool(verdict.get("verdict", {}).get("approved"))
            await fire("reviewed", ctx | {"approved": approved}, cfg, log)
            if not approved:
                log.append({"event": f"review r{rounds}", "approved": False})
                continue

            check = await role("verifier", spec_path=spec_path, impl=impl.get("impl", impl))
            passed = bool(check.get("check", {}).get("passed"))
            await fire("verified", ctx | {"passed": passed}, cfg, log)
            if not passed:
                log.append({"event": f"verify r{rounds}", "passed": False})
                continue

            gate_results = {}
            ok = True
            for gate in stages[stage]:
                result = await role(gate, model=gate_model or model, spec_path=spec_path,
                                    impl=impl.get("impl", impl))
                gate_results[gate] = result.get(gate, result)
                gate_ok = bool(gate_results[gate].get("passed"))
                await fire("gate", ctx | {"gate": gate, "passed": gate_ok}, cfg, log)
                if not gate_ok:
                    ok = False
                    break
            if ok:
                break
        else:
            await fire("failure", ctx, cfg, log)
            return {"ok": False, "reason": "max rounds reached", "stage": stage, "log": log}

        # reaching here means the loop broke on success (review, verification and every gate passed)
        if stage in cfg.get("docs_stages", []):
            docs = await role("documentator", spec_path=spec_path, impl=impl.get("impl", impl),
                              verdict=verdict, check=check, gates=gate_results or None)
            await fire("documented", ctx, cfg, log)

        merged = await role("merger", spec_path=spec_path, impl=impl.get("impl", impl),
                            verdict=verdict, check=check, gates=gate_results or None)
        ctx["head"] = merged.get("merge", {}).get("head") or ctx["head"]
        await fire("merged", ctx, cfg, log)
        await fire("run_end", ctx, cfg, log)
        return {"ok": True, "stage": stage, "rounds": rounds, "head": ctx["head"],
                "gates": gate_results, "spec_path": spec_path, "log": log}
    finally:
        if tracing:
            tracing.event("run-end", stage=stage)
            # close the trace: v4 keeps the overall response on the ROOT observation, not on a trace
            tracing.end_trace(output={"stage": stage, "run_dir": str(run_dir)})
            tracing.flush()
        if not keep_children:
            for handle in children:
                try:
                    await rlm.delete_subagent(handle)
                except Exception:
                    pass


async def run_stages_task(brief_file=None, **kw):
    path = Path(brief_file) if brief_file else WORK_DIR / "task.md"
    return await run_stages(path.read_text(), **kw)


async def run_role_stage(role_id, *, run_dir, values=None, task_kind="default", model=None,
                         models=None, thinking=None, route=False, task_brief="",
                         exclude=None, name=None, on_spawn=None):
    """Render ONE role's prompt and run it as a child agent. Single source of truth, shared by
    run_stages() and by the Temporal worker (temporal_backend.py).

    Returns {"parsed": <the role's json object>, "model": <model used>, "child": <child name>}.
    """
    cfg = load_config()
    roles, _stages = _load_roles()
    prompt = roles[role_id][0].replace(".prime-factory/", f"{run_dir}/")
    for k, v in (values or {}).items():
        prompt = prompt.replace("{" + k + "}", v if isinstance(v, str) else json.dumps(v))
    chain = list(models) if models else ([model] if model else core.models_for(role_id, kind=task_kind))
    if route and model is None:
        try:
            import routing
            pick = await routing.route_or_default(role_id, task_brief, default=chain[0],
                                                  exclude=set(exclude or ()))
            if pick.get("used"):
                chain = [pick["used"]] + [m for m in chain if m != pick["used"]]
        except Exception:
            pass
    child = (name or f"pl-{role_id}-{os.urandom(2).hex()}")[:64]
    handle, parsed, used = await core.run_child(child, prompt, roles[role_id][1], models=chain,
                                           thinking=thinking, on_spawn=on_spawn)
    return {"parsed": parsed, "model": used, "child": handle.name}


