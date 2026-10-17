"""The prebuilt event catalog: canonical points, their timings, and where they may be registered.

A canonical event is a POINT (`tool`, `model`, `commit`); the timing is a separate field
(`before` | `wrap` | `after`). LangChain and Cursor names are accepted as aliases and are parsed into
(point, timing), so `on("before_tool")` == `on("tool", timing="before")`.

Scope decides who may register a hook:
  global   user-level, applies to every project   (~/.prime/agent/hooks/*.json)
  project  committed with the repository          (.prime/hooks/*.json)
  both     either scope may register it

Precedence: global hooks run first and a global `block` cannot be overridden by a project hook.
A project hook may add restrictions, never remove them.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Spec:
    point: str
    timings: tuple = ("before", "after")
    blocking: bool = True          # may a `before` handler stop it? `after` never can.
    anchor: str = ""               # what before/after are measured against for this point
    delivery: str = "inline"       # default delivery when none is given
    scope: str = "both"            # global | project | both
    payload: tuple = ()
    langchain: str = ""
    cursor: str = ""


CATALOG: dict[str, Spec] = {}


def _add(spec: Spec):
    CATALOG[spec.point] = spec


# --- lifecycle --------------------------------------------------------------------------------
_add(Spec("session", ("before", "after"), blocking=False, delivery="inline", scope="both",
          payload=("session", "cwd", "project"), cursor="sessionStart / sessionEnd"))
_add(Spec("agent", ("before", "after"), delivery="inline", scope="both",
          payload=("task", "result"), langchain="before_agent / after_agent",
          cursor="afterAgentResponse"))
_add(Spec("turn", ("before", "after"), delivery="inline", scope="both",
          payload=("turn", "tokens", "corrections")))
_add(Spec("compact", ("before", "after"), blocking=False, delivery="inline", scope="both",
          payload=("pre_tokens", "post_tokens")))
_add(Spec("subagent", ("before", "after"), delivery="inline", scope="both",
          payload=("name", "model", "ok")))

# --- model ------------------------------------------------------------------------------------
_add(Spec("model", ("before", "after", "wrap"), delivery="inline", scope="both",
          payload=("messages", "model", "tools"), langchain="before_model / after_model / wrap_model_call"))
_add(Spec("model_failure", ("after",), delivery="inline", scope="both", payload=("error", "attempt")))

# --- tools ------------------------------------------------------------------------------------
_add(Spec("tool", ("before", "after", "wrap"), delivery="inline", scope="both",
          payload=("tool", "args"), langchain="before_tool / after_tool / wrap_tool_call",
          cursor="preToolUse / postToolUse"))
_add(Spec("tool_failure", ("after",), delivery="inline", scope="both",
          payload=("tool", "error"), cursor="postToolUseFailure"))

# --- shell and files --------------------------------------------------------------------------
_add(Spec("shell", ("before", "after"), delivery="subprocess", scope="both",
          payload=("argv", "cwd", "exit"), cursor="beforeShellExecution / afterShellExecution"))
_add(Spec("file_read", ("before",), delivery="inline", scope="both",
          payload=("path",), cursor="beforeReadFile"))
_add(Spec("file_edit", ("before", "after"), delivery="subprocess", scope="both",
          payload=("path", "diff"), cursor="beforeFileEdit / afterFileEdit"))
_add(Spec("prompt", ("before",), delivery="inline", scope="both",
          payload=("prompt",), cursor="beforeSubmitPrompt"))

# --- git --------------------------------------------------------------------------------------
_add(Spec("commit", ("before", "after"), delivery="subprocess", scope="both",
          payload=("staged", "loc", "files", "message")))
_add(Spec("push", ("before", "after"), delivery="subprocess", scope="both",
          payload=("branch", "remote")))

# --- factory ----------------------------------------------------------------------------------
_add(Spec("run", ("before", "after"), delivery="inline", scope="project", payload=("brief", "ok")))
_add(Spec("stage", ("before", "after"), delivery="inline", scope="project",
          payload=("stage", "passed", "spec")))

# --- learning ---------------------------------------------------------------------------------
_add(Spec("cron", ("before",), blocking=False, delivery="durable", scope="global",
          payload=("name", "schedule")))
_add(Spec("learn", ("before", "after"), blocking=False, delivery="durable", scope="global",
          payload=("session", "turns", "corrections")))
_add(Spec("wiki", ("before", "after"), blocking=False, delivery="inline", scope="both",
          payload=("vault", "created", "updated", "deleted", "skipped")))

# What "before"/"after" are anchored to, per point. "after" is not a generic "later": post_tool is
# after the tool returned, post_model is after the model responded, post_commit is after the commit
# exists. The anchor is what makes the phase unambiguous.
ANCHORS = {
    "session": "the session lifetime",
    "agent": "one agent invocation",
    "turn": "one user turn",
    "compact": "a context compaction",
    "subagent": "one child agent run",
    "model": "a model request and its response",
    "model_failure": "a failed model request",
    "tool": "one tool execution",
    "tool_failure": "a tool that raised or returned an error",
    "shell": "one shell command",
    "file_read": "reading a file",
    "file_edit": "editing a file",
    "prompt": "submitting the user prompt",
    "commit": "creating a git commit",
    "push": "pushing to a remote",
    "run": "one factory run",
    "stage": "one factory stage",
    "cron": "a scheduled tick",
    "learn": "one learning pass",
    "wiki": "one vault sync",
}
for _point, _anchor in ANCHORS.items():
    if _point in CATALOG:
        CATALOG[_point] = replace(CATALOG[_point], anchor=_anchor)

PREFIXES = {"before_": "before", "after_": "after", "wrap_": "wrap"}
# alias -> (point, timing): the names in each spec string are listed in the same order as timings.
ALIASES: dict[str, tuple[str, str]] = {}
for _spec in CATALOG.values():
    for _names in (_spec.langchain, _spec.cursor):
        if not _names:
            continue
        for _alias, _timing in zip([n.strip() for n in _names.split(" / ")], _spec.timings):
            ALIASES[_alias] = (_spec.point, _timing)
# canonical point -> LangChain name for the same point, for the doc table
TABLE = [(spec.point, " / ".join(spec.timings), spec.langchain, spec.cursor,
          spec.blocking, spec.delivery, spec.scope, ", ".join(spec.payload), spec.anchor)
         for spec in CATALOG.values()]


def resolve(name: str, timing: str | None = None, scope: str = "both") -> tuple[str, str]:
    """Alias -> (point, timing). Raises on an unknown point or a timing the point does not support."""
    point = None
    if name.startswith("cron:"):
        point, timing = "cron", timing or "before"
    elif name in ALIASES:
        point, alias_timing = ALIASES[name]
        timing = timing or alias_timing
    elif name in CATALOG:
        point, timing = name, timing or "before"
    else:
        for suffix, kind in (("_start", "before"), ("_begin", "before"),
                             ("_end", "after"), ("_done", "after"), ("_failure", "after")):
            if name.endswith(suffix) and name[:-len(suffix)] in CATALOG:
                point, timing = name[:-len(suffix)], timing or kind
                break
        for prefix, kind in PREFIXES.items():
            rest = name[len(prefix):] if name.startswith(prefix) else ""
            if rest in CATALOG:
                point, timing = rest, timing or kind
                break
    spec = CATALOG.get(point) if point else None
    if spec is None:
        raise ValueError(f"unknown event {name!r}; known points: {sorted(CATALOG)} (or cron:<name>)")
    if timing is None:
        timing = "before"
    if timing not in spec.timings:
        raise ValueError(f"{point!r} does not support timing {timing!r}; supported: {spec.timings}")
    if scope == "project" and spec.scope == "global":
        raise ValueError(f"{point!r} is a global-scope event; it cannot be registered per project")
    return point, timing
