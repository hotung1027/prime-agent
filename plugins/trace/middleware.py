"""Layer 1: middleware around every dispatch - budgets, redaction and one span per handler.

The bus knows nothing about tracing or money; it exposes two seams:

    set_budget_checker(fn)   fn(event, handler) -> bool      called before a handler runs
    set_tracer(fn)           fn(event, handler) -> handle    handle.close(verdict) after it runs

This module installs both. A capability that registers a handler inherits them for free, which is the
entire point: nobody can forget to trace or to cap cost.

Order of operations for one handler:
    budget check -> span open -> invoke -> verdict -> span close (attributes + cost) -> spend record
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

try:
    import bus
except Exception:                                    # imported as core.bus in some contexts
    from core import bus
try:
    import tracing                                   # optional: the local timeline is the fallback
except Exception:
    tracing = None

SECRET = re.compile(r"(sk|pk)-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}"
                    r"|-----BEGIN [A-Z ]*PRIVATE KEY", re.I)
MAX_ATTR_CHARS = 1200


def redact(value, key: str = ""):
    """Nothing secret reaches a trace, and nothing unbounded either."""
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    text = value if isinstance(value, str) else str(value)
    if SECRET.search(text):
        return "[redacted]"
    if re.search(r"secret|token|password|api[_-]?key", key, re.I):
        return "[redacted]"
    return text[:MAX_ATTR_CHARS]


@dataclass
class RootBudget:
    max_usd: float = 0.50          # 0 disables
    max_calls: int = 40
    max_iterations: int = 3


@dataclass
class _Root:
    spend_usd: float = 0.0
    calls: int = 0
    iterations: int = 0


class Middleware:
    def __init__(self, *, budget: RootBudget | None = None, sample: float = 1.0):
        self.budget = budget or RootBudget()
        self.sample = max(0.0, min(1.0, sample))
        self.roots: dict[str, _Root] = {}
        self.counts: dict[tuple[str, str], int] = {}
        self._n = 0
        self._root = __import__("random").random()

    # ---- install -------------------------------------------------------
    def install(self) -> "Middleware":
        bus.set_tracer(self._tracer)
        bus.set_budget_checker(self._check)
        return self

    def uninstall(self):
        bus.set_tracer(None)
        bus.set_budget_checker(None)

    # ---- budget --------------------------------------------------------
    def _root_of(self, event) -> _Root:
        # root_id identifies a causal chain; without one, the session is the chain (one turn's work),
        # so two independent events in the same session share the budget instead of each getting a
        # fresh one.
        rid = event.root_id or event.session or event.key()
        return self.roots.setdefault(rid, _Root())

    def new_turn(self, session: str = ""):
        """Reset the per-turn counters. Call at a turn boundary."""
        for key in [k for k in self.counts if not session or k[0] == session]:
            self.counts.pop(key, None)
        if session:
            self.roots.pop(session, None)

    def _check(self, event, handler) -> bool:
        root = self._root_of(event)
        cap = handler.budget.max_per_turn
        key = (event.root_id or event.session or event.key(), handler.id)
        if cap and self.counts.get(key, 0) >= cap:
            return False                                     # per-handler, per-turn cap
        if self.budget.max_calls and root.calls >= self.budget.max_calls:
            return False                                     # a runaway chain
        if self.budget.max_usd and root.spend_usd >= self.budget.max_usd:
            return False                                     # money
        self.counts[key] = self.counts.get(key, 0) + 1
        root.calls += 1
        return True

    # ---- tracing -------------------------------------------------------
    def _tracer(self, event, handler):
        self._n += 1
        sample = (self._n % max(1, int(1 / self.sample))) == 0 if 0 < self.sample < 1 else True
        attrs = {"event": event.name, "session": event.session, "depth": event.depth,
                 "delivery": handler.delivery, "handler": handler.id,
                 "attrs": redact(event.attrs)}
        if tracing is None or not sample:
            return _NullSpan(self, event, handler)
        cm = tracing.span(f"hook:{event.name}", **attrs)
        sp = cm.__enter__()
        return _Span(cm, sp, self, event, handler)

    # ---- bookkeeping ---------------------------------------------------
    def _record(self, event, verdict):
        root = self._root_of(event)
        root.spend_usd += float(verdict.cost_usd or 0.0)
        if event.depth:
            root.iterations = max(root.iterations, event.depth)
        return root

    def report(self) -> dict:
        return {"roots": len(self.roots),
                "spend_usd": round(sum(r.spend_usd for r in self.roots.values()), 6),
                "calls": sum(r.calls for r in self.roots.values()),
                "worst_root": max((r.spend_usd for r in self.roots.values()), default=0.0)}


class _NullSpan:
    def __init__(self, mw, event, handler):
        self.mw, self.event, self.handler = mw, event, handler

    def close(self, verdict):
        self.mw._record(self.event, verdict)


class _Span:
    def __init__(self, cm, sp, mw, event, handler):
        self.cm, self.sp, self.mw, self.event, self.handler = cm, sp, mw, event, handler

    def close(self, verdict):
        try:
            self.sp.attr(verdict=verdict.verdict, reason=redact(verdict.reason),
                         duration_ms=verdict.duration_ms, cost_usd=float(verdict.cost_usd or 0.0))
            self.cm.__exit__(None, None, None)
        except Exception:
            pass                                     # tracing must never break work
        self.mw._record(self.event, verdict)
