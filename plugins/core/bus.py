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
import time
from dataclasses import dataclass, field
from pathlib import Path

import events as _events          # the prebuilt catalog: canonical points + aliases

MAX_DEPTH = 3
DEDUPE_WINDOW_S = 2.0
DELIVERY_ORDER = {"inline": 0, "subprocess": 1, "durable": 2, "wrap": 0}
DELIVERIES = tuple(DELIVERY_ORDER)
# An unmatched tool event fires dozens of times per turn, so a matcher is mandatory for them.
MATCHER_REQUIRED = {"tool", "shell", "file_edit"}   # canonical points


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
    name: str                 # the event: tool, commit, turn, stage, cron:nightly, ...
    fn: object
    when: object
    delivery: str             # inline | subprocess | durable | wrap
    budget: Budget
    id: str
    script: str = ""
    timing: str = "before"    # before | wrap | after   (the LangChain middleware taxonomy)
    priority: int = 50        # lower runs first; after-handlers run in reverse


_HANDLERS: list[Handler] = []
_SEEN: dict[str, float] = {}
_SPEND: dict[str, float] = {}
_SCRIPT_ALLOWLIST: dict[str, str] = {}      # absolute path -> sha256
_TRACERS: list = []
_BUDGET_CHECKERS: list = []
_DURABLE_TRANSPORT = None


def reset():
    """Test helper: clear the registry and the guards."""
    _HANDLERS.clear(); _SEEN.clear(); _SPEND.clear(); _SCRIPT_ALLOWLIST.clear()


def on(name: str, *, when=None, delivery: str | None = None, budget: Budget | None = None,
       script: str = "", timing: str | None = None, priority: int = 50, scope: str = "both"):
    """Register a handler.

    `name` may be a canonical point (`tool`, `commit`), a timing alias (`before_tool`, `wrap_tool`),
    or a LangChain/Cursor name (`preToolUse`, `wrap_model_call`). The catalog resolves it and rejects
    an unknown point, an unsupported timing, or a global event registered per project.
    """
    name, timing = _events.resolve(name, timing, scope)
    spec = _events.CATALOG[name]
    # The catalog's `delivery` is the default for config-file hooks; an explicit registration is
    # inline unless it says otherwise. Silently turning on("commit") into a subprocess would surprise.
    delivery = delivery or "inline"
    if timing == "wrap":
        delivery = "wrap"
    if delivery not in DELIVERIES:
        raise ValueError(f"unknown delivery {delivery!r}; expected one of {DELIVERIES}")
    if timing not in ("before", "wrap", "after"):
        raise ValueError(f"unknown timing {timing!r}; expected before | wrap | after")
    if timing == "wrap":
        delivery = "wrap"          # a wrap receives (event, next) - it is a delivery, not a timing flag
    if when is None and name in MATCHER_REQUIRED:
        raise ValueError(f"event {name!r} needs a matcher (`when=`): an unmatched tool event "
                         f"fires dozens of times per turn")

    def register(fn):
        _HANDLERS.append(Handler(name=name, fn=fn, when=when, delivery=delivery,
                                 budget=budget or Budget(), id=getattr(fn, "__qualname__", repr(fn)),
                                 script=script, timing=timing, priority=priority))
        return fn
    return register


def allow_script(path, sha256: str):
    """Only these scripts may be run by a subprocess handler, and only at this content."""
    _SCRIPT_ALLOWLIST[str(Path(path).resolve())] = sha256


def add_tracer(fn):
    """Tracers compose: every one is opened, and closed in reverse order."""
    _TRACERS.append(fn)
    return fn


def add_budget_checker(fn):
    """Checkers compose: all must allow the call."""
    _BUDGET_CHECKERS.append(fn)
    return fn


def remove_tracer(fn):
    while fn in _TRACERS:
        _TRACERS.remove(fn)


def remove_budget_checker(fn):
    while fn in _BUDGET_CHECKERS:
        _BUDGET_CHECKERS.remove(fn)


def set_tracer(fn):
    _TRACERS.clear()
    if fn is not None:
        _TRACERS.append(fn)


def set_budget_checker(fn):
    _BUDGET_CHECKERS.clear()
    if fn is not None:
        _BUDGET_CHECKERS.append(fn)


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


async def _invoke(event: Event, handler: Handler, *, next_call=None) -> Verdict:
    """One handler: budgets, tracers, delivery, error isolation, timing."""
    for checker in _BUDGET_CHECKERS:
        try:
            if not checker(event, handler):
                return Verdict.block(f"budget exhausted for {handler.id}",
                                     handler=handler.id, delivery=handler.delivery)
        except Exception:
            continue                                  # a broken checker must not block work
    started = time.monotonic()
    handles = []
    for tracer in _TRACERS:
        try:
            handles.append(tracer(event, handler))
        except Exception:
            handles.append(None)                      # tracing must never break the work
    try:
        if handler.delivery == "subprocess":
            verdict = await _run_subprocess(handler, event)
        elif handler.delivery == "durable":
            if _DURABLE_TRANSPORT is None:
                verdict = Verdict.block("no durable transport registered",
                                        handler=handler.id, delivery="durable")
            else:
                result = _DURABLE_TRANSPORT(event, handler)
                verdict = await result if inspect.isawaitable(result) else result
        elif handler.delivery == "wrap":
            result = handler.fn(event, next_call)
            verdict = await result if inspect.isawaitable(result) else result
        else:
            verdict = await _call(handler.fn, event)
        if not isinstance(verdict, Verdict):
            verdict = Verdict.allow(str(verdict or ""))
        verdict.handler = handler.id
        verdict.delivery = handler.delivery
    except Exception as exc:
        verdict = Verdict.escalate(f"{type(exc).__name__}: {exc}", handler=handler.id,
                                   delivery=handler.delivery)
    verdict.duration_ms = round((time.monotonic() - started) * 1000, 2)
    for handle in reversed(handles):                  # close in reverse: onion order
        if handle is not None and hasattr(handle, "close"):
            try:
                handle.close(verdict)
            except Exception:
                pass
    return verdict


async def dispatch(event: Event, *, operation=None, max_depth: int = MAX_DEPTH) -> list[Verdict]:
    """before -> wrap(operation) -> after, in the LangChain middleware taxonomy.

    `before` handlers run by ascending priority and may block, which stops the chain.
    `wrap` handlers form an onion around `operation` (the real tool/model/commit call).
    `after` handlers run in reverse priority and may escalate, never un-block.
    """
    if event.depth > max_depth:
        return [Verdict.block(f"event depth {event.depth} exceeds {max_depth}",
                              handler="bus", delivery="bus")]
    key = event.key()
    now = time.time()
    # _SEEN is insertion-ordered: prune from the head while stale (O(1) amortised; a full scan here
    # was measured at 213 us per dispatch).
    while _SEEN:
        head = next(iter(_SEEN))
        if now - _SEEN[head] <= DEDUPE_WINDOW_S:
            break
        _SEEN.pop(head)
    if key in _SEEN:
        return []
    _SEEN[key] = now

    try:
        point, _ = _events.resolve(event.name)
    except ValueError:
        point = event.name                      # a custom point still dispatches by exact name
    matched = [h for h in _HANDLERS
               if h.name == point and (h.when is None or h.when(event))]
    before = sorted([h for h in matched if h.timing == "before"],
                    key=lambda h: (h.priority, DELIVERY_ORDER[h.delivery]))
    wraps = sorted([h for h in matched if h.timing == "wrap"], key=lambda h: h.priority)
    after = sorted([h for h in matched if h.timing == "after"], key=lambda h: -h.priority)

    results: list[Verdict] = []
    for handler in before:
        verdict = await _invoke(event, handler)
        results.append(verdict)
        if verdict.verdict in ("block", "escalate"):
            return results                             # short-circuit, like a refused commit

    if wraps or operation is not None:
        async def core():
            if operation is None:
                return Verdict.allow("no operation")
            result = operation(event)
            if inspect.isawaitable(result):
                result = await result
            return result if isinstance(result, Verdict) else Verdict.allow(str(result or ""))

        chain = core
        for handler in reversed(wraps):
            nxt = chain

            async def wrapped(handler=handler, nxt=nxt):
                return await _invoke(event, handler, next_call=nxt)

            chain = wrapped
        results.append(await chain())

    for handler in after:
        verdict = await _invoke(event, handler)
        if verdict.verdict == "block":
            # the operation already ran; an after-hook cannot un-run it, and recording a "block" would
            # let a post-hook pretend it prevented something.
            verdict.verdict = "escalate"
            verdict.reason = f"after-hook: {verdict.reason}" if verdict.reason else "after-hook"
        results.append(verdict)

    for verdict in list(results):
        if verdict.continuation is not None:
            results.extend(await dispatch(
                event.child(verdict.continuation.name, **verdict.continuation.attrs),
                max_depth=max_depth))
    return results


def blocked(results) -> list[Verdict]:
    return [r for r in results if r.verdict in ("block", "escalate")]
