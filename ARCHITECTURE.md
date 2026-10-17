# Architecture of this version

Three layers, each with one owner. The runtime executes; the harness is what it remembers; the plugins
are what it can do beyond the built-ins.

```
prime-agent-runtime/src/rlm/     the runtime (upstream): repl, harness, factory, skill loader
        ^                  ^
        | reads            | imports
   harness/            plugins/
   prompts, memories,  factory, durable_function, trace,
   skills, machines    decision_layer, continual_learning, llm_wiki, batch, tools
```

## 1. Runtime (`prime-agent-runtime/src/rlm/`)

Upstream code, not modified by this version. The parts that matter here:

- `harness.py` - owns the entries: `prompt`, `memory`, `skill`, `subagent`, `factory`. Survivors of
  compaction, reloaded into every session.
- `factory.py` - the state-machine orchestrator (`run`, `status`, `stop`, `resume`, `graph`, `watch`).
  Machines are `MACHINE.md` specs; bundled seeds are `builder`, `pr-manager`, `review-sweep`.
- `skill.py` - discovers `SKILL.md` packages and exposes them as callable tools.
- `repl.py` - the Python kernel the model works in.

Targeted version: **0.10.0** (`~/.local/share/prime-agent/.prime-agent-install`). The repo's `main`
is ahead of that build in `factory.py`, `harness.py` and `__init__.py`, so check the runtime version
before assuming an API.

## 2. Harness (`harness/`)

Data, not code. Five prompt notes (orchestration and verification policy), twenty-three memories
(durable technical facts), seven skills, two machines (`staged-delivery`, `feature-pipeline`).
Scrubbed of personal paths; install with `cp -r` into the agent dir (see `harness/README.md`).

## 3. Plugins (`plugins/`)

One plugin per capability, no orchestration in a leaf:

| plugin | depends on | owns |
|---|---|---|
| `factory` | trace | the staged engine (`engine.py`, `engine.json`), the staged-delivery machine, roles, runners, routing |
| `durable_function` | factory, trace | optional Temporal layer over the factory engine (Python SDK) + optional Rust activity worker |
| `trace` | - | local timeline + Langfuse v4 OTLP |
| `decision_layer` | factory, trace | choice / score / noul + MCP server |
| `continual_learning` | batch, decision_layer, factory, trace | daily reflection |
| `llm_wiki` | - | vault mirror |
| `batch` | - | offline submission |
| `tools` | - | model catalog cache, cost/speed economics |

Entry points per plugin: `plugins/API.md`. The dependency column is the flat-install reality: modules import each other by name, so the agent dir
must contain all of them. `plugins/README.md` documents the install.

## Scope

This document covers the runtime, the harness and the plugins. Anything outside those three layers
is out of scope here.
