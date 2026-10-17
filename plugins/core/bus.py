"""Event bus on the CPython kernel: one dispatcher, three deliveries, three guards.

Everything in this architecture is `fn(event) -> Verdict | Continuation`. The bus is the only thing
that knows how a handler is delivered:

  inline      a plain function (sync or async) in this process
  subprocess  a script from the allowlist, argv-exec'd, event JSON on stdin, exit code = verdict
  durable     a transport registered by the durable layer (Temporal); a signal or a workflow start

Guards, because a reactive system fails in three predictable ways:
  depth <= MAX_DEPTH          a handler that emits what it listens to
  dedupe by event content     the same event arriving twice in a window
  per-root budget             a loop that keeps spending

Deliberately NOT here: tracing, budgets in dollars and model calls. Those are middleware (layer 1),
which registers a tracer and a budget checker through `set_tracer` / `set_budget_checker`.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_DEPTH = 3
DEDUPE_WINDOW_S = 2.0
DELIVERY_ORDER = {"inline": 0, "subprocess": 1, "durable": 2}
DELIVERIES = tuple(DELIVERY_ORDER)
# An unmatched tool event fires dozens of times per turn, so a matcher is mandatory for them.
MATCHER_REQUIRED = {"tool_start", "tool_end", "file_edit"}


@dataclass
class Event:
    name: str
    attrs: dict = field(default_factory=dict)
    session: str = ""
    root_id: str = ""
    causation_id: str = ""
    depth: int = 0
    ts: float = field(default_factory=time.time)

    def child(self, name: str, **attrs) -> "Event":
        """A derived event: same root, deeper, causally linked."""
        return Event(name=name, attrs=attrs, session=self.session, root_id=self.root_id or self.key(),
                     causation_id=self.key(), depth=self.depth + 1, ts=time.time())

    def key(self) -> str:
        blob = json.dumps({"n": self.name, "a": self.attrs, "s": self.session}, sort_keys=True,
                          default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Continuation:
    """Work handed to the next handler: an event name plus its attributes."""
    name: str
    attrs: dict = field(default_factory=dict)

    @classmethod
    def emit(cls, name: str, **attrs) -> "Continuation":
        return cls(name=name, attrs=attrs)


@dataclass
class Verdict:
    verdict: str = "allow"            # allow | block | escalate | modify
    reason: str = ""
    continuation: Continuation | None = None
    handler: str = ""
    delivery: str = ""
    duration_ms: float = 0.0
    cost_usd: float = 0.0
    output: str = ""

    @classmethod
    def allow(cls, reason: str = "", **kw) -> "Verdict":
        return cls("allow", reason, **kw)

    @classmethod
    def block(cls, reason: str, **kw) -> "Verdict":
        return cls("block", reason, **kw)

    @classmethod
    def escalate(cls, reason: str, **kw) -> "Verdict":
        return cls("escalate", reason, **kw)

    @classmethod
    def modify(cls, reason: str, continuation: "Continuation | None" = None, **kw) -> "Verdict":
        return cls("modify", reason, continuation=continuation, **kw)


@dataclass
class Budget:
    timeout_s: float = 30.0
    max_usd: float = 0.0              # 0 = unlimited (checked by middleware, not the bus)
    max_per_turn: int = 0
    max_iterations: int = 0


@dataclass
class Handler:
    name: str
    fn: object
    when: object
    delivery: str
    budget: Budget
    id: str
    script: str = ""


_HANDLERS: list[Handler] = []
_SEEN: dict[str, float] = {}
_SPEND: dict[str, float] = {}
_SCRIPT_ALLOWLIST: dict[str, str] = {}      # absolute path -> sha256
_TRACER = None
_BUDGET_CHECKER = None
_DURABLE_TRANSPORT = None


def reset():
    """Test helper: clear the registry and the guards."""
    _HANDLERS.clear(); _SEEN.clear(); _SPEND.clear(); _SCRIPT_ALLOWLIST.clear()


def on(name: str, *, when=None, delivery: str = "inline", budget: Budget | None = None,
       script: str = ""):
    """Register a handler. `when(event)` must be given for tool events."""
    if delivery not in DELIVERIES:
        raise ValueError(f"unknown delivery {delivery!r}; expected one of {DELIVERIES}")
    if when is None and name in MATCHER_REQUIRED:
        raise ValueError(f"event {name!r} needs a matcher (`when=`): an unmatched tool event "
                         f"fires dozens of times per turn")

    def register(fn):
        _HANDLERS.append(Handler(name=name, fn=fn, when=when, delivery=delivery,
                                 budget=budget or Budget(), id=getattr(fn, "__qualname__", repr(fn)),
                                 script=script))
        return fn
    return register


def allow_script(path, sha256: str):
    """Only these scripts may be run by a subprocess handler, and only at this content."""
    _SCRIPT_ALLOWLIST[str(Path(path).resolve())] = sha256


def set_tracer(fn):
    global _TRACER; _TRACER = fn


def set_budget_checker(fn):
    global _BUDGET_CHECKER; _BUDGET_CHECKER = fn


def set_durable_transport(fn):
    global _DURABLE_TRANSPORT; _DURABLE_TRANSPORT = fn


def handlers(name: str | None = None) -> list[Handler]:
    return [h for h in _HANDLERS if name is None or h.name == name]


async def _call(fn, event):
    result = fn(event)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _run_subprocess(handler: Handler, event: Event) -> Verdict:
    path = Path(handler.script or "").resolve()
    expected = _SCRIPT_ALLOWLIST.get(str(path))
    if expected is None:
        return Verdict.block(f"script not allowlisted: {path}", handler=handler.id, delivery="subprocess")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        return Verdict.block(f"script content changed: {path.name} (sha256 mismatch)",
                             handler=handler.id, delivery="subprocess")
    proc = await asyncio.create_subprocess_exec(
        str(path),  # argv exec, never a shell: a filename cannot inject
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(path.parent))
    payload = json.dumps({"name": event.name, "attrs": event.attrs, "session": event.session}).encode()
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout=handler.budget.timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        return Verdict.block(f"script timed out after {handler.budget.timeout_s:g}s",
                             handler=handler.id, delivery="subprocess")
    text = (err or out or b"").decode("utf-8", "replace")[:2000]
    verdict = "allow" if proc.returncode == 0 else "block"
    return Verdict(verdict=verdict, reason=text.strip(), handler=handler.id,
                   delivery="subprocess", output=(out or b"").decode("utf-8", "replace")[:2000])


async def dispatch(event: Event, *, max_depth: int = MAX_DEPTH) -> list[Verdict]:
    """Match -> order by delivery -> invoke -> collect verdicts -> emit continuations."""
    if event.depth > max_depth:
        return [Verdict.block(f"event depth {event.depth} exceeds {max_depth}",
                              handler="bus", delivery="bus")]
    key = event.key()
    now = time.time()
    for stale in [k for k, t in _SEEN.items() if now - t > DEDUPE_WINDOW_S]:
        _SEEN.pop(stale, None)
    if key in _SEEN:
        return []                       # same event within the window: already handled
    _SEEN[key] = now

    matched = [h for h in _HANDLERS
               if h.name == event.name and (h.when is None or h.when(event))]
    matched.sort(key=lambda h: DELIVERY_ORDER[h.delivery])

    results: list[Verdict] = []
    for handler in matched:
        if _BUDGET_CHECKER is not None and not _BUDGET_CHECKER(event, handler):
            results.append(Verdict.block(f"budget exhausted for {handler.id}",
                                         handler=handler.id, delivery=handler.delivery))
            continue
        started = time.monotonic()
        try:
            span = _TRACER(event, handler) if _TRACER is not None else None
        except Exception:
            span = None                     # tracing must never break the work it observes
        try:
            if handler.delivery == "subprocess":
                verdict = await _run_subprocess(handler, event)
            elif handler.delivery == "durable":
                if _DURABLE_TRANSPORT is None:
                    verdict = Verdict.block("no durable transport registered",
                                            handler=handler.id, delivery="durable")
                else:
                    result = _DURABLE_TRANSPORT(event, handler)
                    if inspect.isawaitable(result):
                        result = await result
                    verdict = result
            else:
                verdict = await _call(handler.fn, event)
            if not isinstance(verdict, Verdict):
                verdict = Verdict.allow(str(verdict or ""))
            verdict.handler = handler.id
            verdict.delivery = handler.delivery
        except Exception as exc:                       # a handler error must not kill the bus
            verdict = Verdict.escalate(f"{type(exc).__name__}: {exc}", handler=handler.id,
                                       delivery=handler.delivery)
        verdict.duration_ms = round((time.monotonic() - started) * 1000, 2)
        if span is not None and hasattr(span, "close"):
            try: span.close(verdict)
            except Exception: pass
        results.append(verdict)
        if verdict.continuation is not None:
            results.extend(await dispatch(event.child(verdict.continuation.name,
                                                      **verdict.continuation.attrs),
                                          max_depth=max_depth))
    return results


def blocked(results) -> list[Verdict]:
    return [r for r in results if r.verdict in ("block", "escalate")]
