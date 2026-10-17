"""Hook configuration: declare hooks in JSON, load them into the bus.

Two scopes, one loader:

    ~/.prime/agent/hooks/*.json     global  - applies to every project and session
    .prime/hooks/*.json            project - committed with the repository

A file looks like:

    {
      "scope": "project",
      "hooks": [
        {"event": "commit", "timing": "before", "delivery": "subprocess",
         "script": ".githooks/check_commit_size.sh", "priority": 10},
        {"event": "BeforeToolUse", "when": "^git ", "callback": "core.hooks:warn_on_force_push"},
        {"event": "turn", "timing": "after", "delivery": "durable", "callback": "core.hooks:schedule_learn"}
      ]
    }

Security, because a config file is executable in effect:

- a callback reference is a module path (`mod:func`) that must be imported from an ALLOWED module, or a
  file inside the repository. A note in the vault, or any file outside the repo, cannot be a callback.
- a `script` is pinned by sha256 at load time, so swapping the file afterwards is refused.
- an unknown event, an unsupported timing, a global-only event in a project file: all refused loudly.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path

try:
    import bus
except Exception:                                  # imported as core.bus
    from core import bus

ALLOWED_MODULES = ("core.", "trace.", "factory.", "continual_learning.", "llm_wiki.", "batch.",
                   "tools.", "hooks_", "decision_layer.", "durable_function.")
ROOT = Path(__file__).resolve().parent.parent.parent      # the repository root


class HookConfigError(Exception):
    pass


def _load_module_file(path: Path):
    spec = importlib.util.spec_from_file_location(f"hookmod_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def resolve_callback(ref: str, *, base: Path | None = None):
    """`mod:func` or `path.py:func`, restricted to allowed modules and files inside the repository."""
    if not ref or ":" not in ref:
        raise HookConfigError(f"callback must be 'module:function', got {ref!r}")
    target, _, func = ref.partition(":")
    if target.endswith(".py"):
        path = (base / target).resolve() if base else Path(target).resolve()
        if ROOT not in path.parents:
            raise HookConfigError(f"callback file outside the repository: {path}")
        module = _load_module_file(path)
    else:
        if not target.startswith(ALLOWED_MODULES):
            raise HookConfigError(f"module {target!r} is not an allowed callback source")
        module = importlib.import_module(target)
    try:
        return getattr(module, func)
    except AttributeError:
        raise HookConfigError(f"{target} has no {func!r}") from None


def pin_script(script: str, *, base: Path | None = None) -> str:
    """Allowlist a script by content. Swapping the file afterwards is refused at dispatch."""
    path = (base / script).resolve() if base else Path(script).resolve()
    if not path.exists():
        raise HookConfigError(f"script not found: {path}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    bus.allow_script(path, digest)
    return digest


def load_file(path, *, scope: str | None = None) -> list[dict]:
    """Register every hook in one config file. Returns the specs that were registered."""
    path = Path(path)
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        raise HookConfigError(f"{path}: {type(exc).__name__}: {exc}") from None
    scope = scope or data.get("scope") or "project"
    base = path.parent
    registered = []
    for spec in data.get("hooks") or []:
        event = spec.get("event")
        if not event:
            raise HookConfigError(f"{path}: a hook without an event")
        callback = resolve_callback(spec["callback"], base=base) if spec.get("callback") else None
        if callback is None and not spec.get("script"):
            raise HookConfigError(f"{path}: hook {event!r} has neither callback nor script")
        if callback is None:
            callback = lambda event, _p=path: bus.Verdict.allow(f"script hook from {_p.name}")
        if spec.get("script"):
            pin_script(spec["script"], base=base)
        try:
            bus.add(event, callback, timing=spec.get("timing"), when=spec.get("when"),
                    delivery=spec.get("delivery"), priority=int(spec.get("priority", 50)),
                    scope=scope, script=spec.get("script", ""))
        except ValueError as exc:
            raise HookConfigError(f"{path}: {exc}") from None
        registered.append({**spec, "scope": scope, "source": str(path)})
    return registered


def load_dir(directory, *, scope: str | None = None) -> list[dict]:
    directory = Path(directory).expanduser()
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.json")):
        out.extend(load_file(path, scope=scope))
    return out


def load_all(global_dir=None, project_dir=None, *, cwd=None) -> dict:
    """Global hooks first, then project hooks. A project hook can add restrictions, not remove them."""
    cwd = Path(cwd or Path.cwd())
    global_dir = Path(global_dir or Path.home() / ".prime" / "agent" / "hooks")
    project_dir = Path(project_dir or cwd / ".prime" / "hooks")
    g = load_dir(global_dir, scope="global")
    p = load_dir(project_dir, scope="project")
    return {"global": len(g), "project": len(p), "total": len(g) + len(p),
            "global_dir": str(global_dir), "project_dir": str(project_dir)}
