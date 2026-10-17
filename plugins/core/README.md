# core - the event bus and the shared primitives

## Subscribe (pub/sub)

```python
import bus

bus.add("tool", callback=lint, timing="before", when=r"^git ")      # regex filter
bus.add("commit", callback=size_gate, timing="before")               # filter optional for commit
bus.add("turn", callback=learn, timing="after", delivery="durable")  # durable transport
bus.add("model", callback=audit, timing="wrap")                      # wrap the call
```

`add(event, callback, *, timing, when, delivery, priority, scope, budget, script)`

- **event** - a canonical point (`tool`, `model`, `commit`, `turn`, `session`, `stage`, `cron:nightly`)
  or an alias: `pre_tool`, `post_model`, `during_tool`, `wrap_tool_call`, `preToolUse`, `sessionStart`.
- **when** - the filter: `callable(event) -> bool`, a **regex** matched against `event.text()` (the
  command, tool name or message), or a `{field: regex}` dict. Mandatory for `tool`, `shell`,
  `file_edit` - an unmatched high-frequency event fires dozens of times per turn.
- **timing** - `before` (may block), `wrap` (may replace the result), `after` (observe/escalate only;
  the operation already ran).
- **delivery** - `inline` (function), `subprocess` (allowlisted, sha256-pinned script) or `durable`
  (transport registered by the durable layer).

`@bus.on(...)` is the decorator form of the same call.

## Publish

```python
verdicts = await bus.dispatch(bus.Event("tool", {"tool": "bash", "command": "git commit -m x"}),
                              operation=lambda e: run_the_tool(e))
```

`before` handlers run by ascending priority and may short-circuit; `wrap` handlers form an onion
around `operation`; `after` handlers run in reverse priority. A handler that returns a `Continuation`
re-enters the bus as a child event (depth-capped).

## Guards

depth <= 3 · content-hash dedupe (2 s) · per-handler budget hook · a tracer or budget checker that
raises cannot break the work.

## Files

| file | role |
|---|---|
| `bus.py` | Event / Verdict / Continuation / Budget, `add` / `on` / `dispatch` |
| `events.py` | the prebuilt catalog: points, timings, anchors, scopes, LangChain/Cursor aliases |
| `core.py` | model-chain resolution, the settings file, spawning one child until it answers |
