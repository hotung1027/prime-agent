
"""Deterministic feature pipeline runner for Prime Agent.

Drives the plan -> implement -> review -> verify -> merge roles of the
`feature-pipeline` machine template, but instead of trusting the factory's
answer-preview capture (or the unreliable collect() "settled" flag) it waits
until each child's session file actually contains the answer it needs, then
reads that answer from the file.

Usage (Python kernel):
    import os
    exec(open(os.path.expanduser("~/.prime/agent/pipelines/machine_runner.py")).read())
    result = await run_stages("Add a --json flag to auth.py")
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import rlm

WORK_DIR = Path(".prime-factory")
PIPELINE = "feature-pipeline"
MAX_ROUNDS = 3
ROLE_TIMEOUT_S = 900.0
POLL_S = 2.0


def _machine_prompts() -> dict:
    """Role prompts from the machine template (single source of truth)."""
    import rlm.factory as F
    machine, _ = F.resolve_machine(PIPELINE)
    return {s["id"]: s["subagent"]["prompt"] for s in machine.spec["states"]}


def _session_rows(session_dir):
    paths = [p for p in Path(session_dir).glob("*.jsonl") if not p.name.startswith("semantic")]
    if not paths:
        return []
    rows = []
    for line in paths[0].read_text().splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def _first_json_object(text: str):
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start : i + 1])
                except ValueError:
                    start = None
    return None


def _last_assistant_text(session_dir):
    for row in reversed(_session_rows(session_dir)):
        if row.get("type") != "message":
            continue
        msg = row.get("message", {})
        if msg.get("role") != "assistant" or msg.get("stopReason") == "aborted":
            continue
        for block in msg.get("content", []):
            if block.get("type") == "text" and block.get("text", "").strip():
                return block["text"]
    return None


def _child_status(session_dir):
    for row in reversed(_session_rows(session_dir)):
        st = (row.get("state") or {}).get("status") if row.get("type") == "session_state" else None
        if st:
            return st
    return None


async def _run_role(prompt, name, *, key, model=None, thinking=None, timeout=ROLE_TIMEOUT_S):
    """Spawn one role child and wait until its session holds the answer we need."""
    handle = await rlm.spawn(prompt, name=name, model=model, thinking=thinking)
    started = time.monotonic()
    while True:
        text = _last_assistant_text(handle.session_dir)
        if text is not None:
            if key is None:
                return handle, text
            parsed = _first_json_object(text)
            if isinstance(parsed, dict) and key in parsed:
                return handle, parsed
        if _child_status(handle.session_dir) == "archived":
            raise RuntimeError(f"role {name} was archived before producing {key or 'an answer'}")
        if time.monotonic() - started > timeout:
            raise TimeoutError(f"role {name} produced no {key or 'answer'} in {timeout:.0f}s")
        await asyncio.sleep(POLL_S)


async def run_stages(task_brief, *, work_dir=WORK_DIR, max_rounds=MAX_ROUNDS,
                       keep_children=False, model=None, reviewer_model=None):
    """Run the machine on one task brief. Returns a summary dict."""
    prompts = _machine_prompts()
    wd = Path(work_dir)
    wd.mkdir(parents=True, exist_ok=True)
    (wd / "task.md").write_text(task_brief)

    run_tag = os.urandom(3).hex()
    children = []
    log = []

    async def role(state_id, prompt, *, key, **kw):
        name = f"fp-{state_id}-{run_tag}-r{len(log)}"[:64]
        handle, value = await _run_role(prompt, name, key=key, **kw)
        children.append(handle)
        return value

    def render(state_id, **values):
        text = prompts[state_id]
        for k, v in values.items():
            text = text.replace("{" + k + "}", json.dumps(v))
        return text

    try:
        spec = await role("plan", prompts["plan"], key="spec_path")
        spec_path = spec.get("spec_path", str(wd / "spec.md"))
        log.append("plan")

        verdict = check = None
        impl = None
        approved = passed = False
        for rounds in range(1, max_rounds + 1):
            impl = await role("implement", render("implement", spec_path=spec_path,
                                                  verdict=verdict, check=check), key="impl")
            log.append(f"implement r{rounds}")

            verdict = await role("review", render("review", spec_path=spec_path,
                                                  impl=impl.get("impl", impl)), key="verdict",
                                 model=reviewer_model, thinking="high")
            approved = bool(verdict.get("verdict", {}).get("approved"))
            log.append(f"review r{rounds} approved={approved}")
            if not approved:
                continue

            check = await role("verify", render("verify", spec_path=spec_path,
                                                impl=impl.get("impl", impl)), key="check")
            passed = bool(check.get("check", {}).get("passed"))
            log.append(f"verify r{rounds} passed={passed}")
            if passed:
                break
        else:
            return {"ok": False, "reason": "max rounds reached", "log": log}

        if not (approved and passed):
            return {"ok": False, "reason": f"approved={approved} passed={passed}", "log": log}

        report = await role("merge", render("merge", spec_path=spec_path,
                                            impl=impl.get("impl", impl), verdict=verdict,
                                            check=check), key=None)
        log.append("merge")
        return {"ok": True, "report": report, "spec_path": spec_path, "log": log, "rounds": rounds}
    finally:
        if not keep_children:
            for handle in children:
                try:
                    await rlm.delete_subagent(handle)
                except Exception:
                    pass


async def run_stages_task(brief_file=None, **kw):
    """Run from a brief file (default .prime-factory/task.md)."""
    path = Path(brief_file) if brief_file else WORK_DIR / "task.md"
    return await run_stages(path.read_text(), **kw)
