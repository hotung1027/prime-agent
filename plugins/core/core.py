"""Bottom layer: the primitives every plugin needs, and nothing else.

Model-chain resolution, the settings file, and spawning one child agent until it returns the
JSON answer the caller asked for. No stage, hook, gate or prompt logic lives here, so every
other plugin can depend on this module without depending on the orchestration engine.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

import rlm
import agent_message

HERE = (Path(__file__).resolve().parent if "__file__" in globals()
        else Path(os.path.expanduser("~/.prime/agent/pipelines")))
SETTINGS_PATH = HERE / "settings.json"
POLL_S = 2.0
ROLE_TIMEOUT_S = 900.0
ERROR_GRACE_S = 120.0


def load_settings() -> dict:
    """The shared settings file: model tiers and the decision rule."""
    try:
        return json.loads(SETTINGS_PATH.read_text())
    except Exception:
        return {}


def models_for(role, cfg=None, kind="default"):
    """Fallback chain for a role: settings 'roles.<role>' (a list, or a map keyed by kind)."""
    models = (cfg if cfg is not None else load_settings()) or {}
    roles = models.get("roles") or {}
    entry = roles.get(role, roles.get("default"))
    if isinstance(entry, dict):
        entry = entry.get(kind) or entry.get("default")
    if isinstance(entry, str):
        entry = [entry]
    if entry:
        return [m for m in entry if m] or [models.get("default")]
    return [models.get("default")]


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


def _first_json_object(text):
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


def _child_error(session_dir):
    """The last assistant message carries a provider stream failure?"""
    for row in reversed(_session_rows(session_dir)):
        if row.get("type") != "message":
            continue
        msg = row.get("message", {})
        if msg.get("role") != "assistant":
            continue
        if msg.get("stopReason") == "error":
            for diag in msg.get("diagnostics") or []:
                if diag.get("type") == "provider_stream_failure":
                    return str((diag.get("error") or {}).get("message") or "provider stream failure")[:200]
            return "assistant turn ended with stopReason=error"
        return None            # the last assistant message is clean
    return None


def _child_archived(session_dir):
    for row in reversed(_session_rows(session_dir)):
        if row.get("type") == "session_state":
            return (row.get("state") or {}).get("status") == "archived"
    return False


def _session_signature(session_dir):
    """(message count, mtime) — used to detect real progress, not wall-clock time."""
    paths = [p for p in Path(session_dir).glob("*.jsonl") if not p.name.startswith("semantic")]
    if not paths:
        return (0, 0)
    try:
        return (sum(1 for _ in paths[0].open()), paths[0].stat().st_mtime_ns)
    except OSError:
        return (0, 0)


def _agent_reply(session_dir, key):
    """Fallback answer: the argument of the child's last agent_message.send(...) tool call."""
    for row in reversed(_session_rows(session_dir)):
        if row.get("type") != "message":
            continue
        msg = row.get("message", {})
        if msg.get("role") != "assistant":
            continue
        for block in msg.get("content", []):
            if block.get("type") != "toolCall" or block.get("name") not in ("ipython", "agent_message"):
                continue
            blob = json.dumps(block.get("arguments", {}))
            if "agent_message" not in blob:
                continue
            for candidate in re.findall(r'\{.*?\}', blob.replace('\\"', '"')):
                parsed = _first_json_object(candidate)
                if isinstance(parsed, dict) and key in parsed:
                    return parsed
    return None


async def run_child(name, prompt, key, *, model=None, models=None, thinking=None,
                    timeout=ROLE_TIMEOUT_S, on_spawn=None):
    """Spawn one role child and wait until its session holds the answer we need.

    `models` is a fallback chain (primary first). The timeout is an INACTIVITY budget: it only
    fires when the child's session stops growing for `timeout` seconds, so a long build - or a
    suspended laptop - never fails a working child. Returns (handle, parsed, model_used).
    """
    chain = list(models) if models else ([model] if model else [None])
    errors = []
    for i, mdl in enumerate(chain):
        spawn_name = (name if i == 0 else f"{name}-f{i}")[:64]
        try:
            handle = await rlm.spawn(prompt, name=spawn_name, model=mdl, thinking=thinking)
            if on_spawn is not None:
                try:
                    on_spawn(handle)
                except Exception:
                    pass
        except Exception as exc:
            errors.append(f"{mdl or 'default'}: spawn failed ({type(exc).__name__}: {exc})")
            continue
        last_change = time.monotonic()
        signature = None
        error_since = None
        while True:
            current = _session_signature(handle.session_dir)
            if current != signature:
                signature = current
                last_change = time.monotonic()
                error_since = None          # progress: any earlier stream error was retried
            text = _last_assistant_text(handle.session_dir)
            if text is not None:
                parsed = _first_json_object(text)
                if isinstance(parsed, dict) and key in parsed:
                    return handle, parsed, mdl
            replied = _agent_reply(handle.session_dir, key)
            if replied is not None:
                return handle, replied, mdl
            failure = _child_error(handle.session_dir)
            if failure is not None:
                # A stream failure is NOT terminal on its own: the harness retries it internally, and
                # bailing out immediately spawned a spurious fallback 71 s into a healthy 9-minute
                # verifier run. Only treat it as terminal if the child stops progressing.
                if error_since is None:
                    error_since = time.monotonic()
                elif time.monotonic() - error_since > ERROR_GRACE_S:
                    errors.append(f"{mdl or 'default'}: provider stream failure ({failure}), "
                                  f"no progress for {ERROR_GRACE_S:.0f}s")
                    break
            else:
                error_since = None
            if _child_archived(handle.session_dir):
                errors.append(f"{mdl or 'default'}: archived/failed before producing {key}")
                break
            if time.monotonic() - last_change > timeout:
                errors.append(f"{mdl or 'default'}: no progress for {timeout:.0f}s")
                break
            await asyncio.sleep(POLL_S)
    raise RuntimeError(f"role {name} produced no {key} on any model: " + "; ".join(errors))


_run_role = run_child        # compatibility alias for callers written before the split


async def stop_child(name):
    """Stop a child agent: tell it to stop, then try to remove it.

    Used when a Temporal activity is cancelled. A cancelled activity MUST take its child down, or the
    retry spawns a second child and two agents write the same files concurrently.
    """
    try:
        await agent_message.send(
            "STOP: the activity that spawned you was cancelled. Stop work immediately and do "
            "not write any file. Reply with one line saying what you had already changed.",
            receiver_role="child", receiver_name=name)
    except Exception:
        pass
    try:
        await rlm.delete_subagent(name)
    except Exception:
        pass
    return name
