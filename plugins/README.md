# Plugins

One plugin per capability. The runtime loads a flat directory (the agent dir); this tree is the
source of truth, organised by responsibility so a change lands in exactly one plugin.

| plugin | owns |
|---|---|
| `factory/` | multi-agent orchestration: the staged engine (`engine.py` + `engine.json`), the machine, role prompts, runners, routing |
| `durable_function/` | durable execution: the Temporal layer over the factory engine - **Python SDK**; optional **Rust** activity worker |
| `trace/` | observability: local timeline + Langfuse v4 OTLP export |
| `decision_layer/` | the decision primitives (choice / score / noul) and their MCP server |
| `continual_learning/` | daily reflection over recorded traces |
| `llm_wiki/` | the knowledge-vault mirror |
| `batch/` | offline batch submission |
| `tools/` | remaining leaf utilities: model catalog cache and cost/speed economics |

Every entry point each plugin exposes - 75 callables - is listed in **[API.md](API.md)**.

Install: link each plugin's files into the agent dir (flat), so module imports resolve by name:

    for d in factory durable_function trace decision_layer continual_learning llm_wiki batch tools; do
      ln -sf "$PWD/$d"/* ~/.prime/agent/pipelines/   # the agent dir's flat module directory
    done

Dependency notes (visible because the install is flat):

- `decision_layer` imports `engine.load_config` / `_models_for` / `_run_role` from
  `durable_function` to spawn its judge child. Follow-up: call the factory instead of a private helper.
- `continual_learning` imports `batch`, `decision`, `pipeline` and `tracing`.
- `trace` has no dependencies; everything else may import it.

No credentials are stored here - see `SECURITY.md`. Versions: `VERSIONS.md`, `VERSION`, `CHANGELOG.md`.

## Runtime limits

A daemon session interrupts a `python_repl` cell at `pythonCellTimeoutMs`. The default is 2 hours (`7200000`); `0` disables the limit. Factory runners such as `run_child` and `run_stages` poll a child inside that one cell for the whole staged run, so the previous 60 second default cut the cell off. A per-call `timeout` on the tool cannot ask for longer than the same 2 hour cap. Daemonless sessions do not arm the limit.

`await bash(cmd)` returns only after the command finishes. The result's `exit_code` is the process status (`BashHandle.exit_code` reads the same value). Shell hooks in `factory/engine.py` record that status. The previous daemon behavior returned a still-running snapshot after 1 second and left `exit_code` unset. A handle that is not awaited still runs on, and its `[bash-done]` notice arrives when it finishes.
