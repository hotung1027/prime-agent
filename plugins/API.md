# API reference

Public entry points per plugin, generated from the source (top-level callables, docstrings).

## `trace`

| file | function | purpose |
|---|---|---|
| `tracing.py` | `status` | is Langfuse enabled, where the local timeline is |
| `tracing.py` | `start_trace(name, *, session_id, user_id, tags, input, output, version, release, environment, metadata)` | Begin a named trace. |
| `tracing.py` | `end_trace(output, error)` | Close the trace by putting the overall response on the root observation. |
| `tracing.py` | `event(name, kind)` | record a point event (started and ended at once) |
| `tracing.py` | `span(name)` | context manager for a timed span with attributes |
| `tracing.py` | `flush(verbose)` | Push queued spans. OTLP is the only export path in v4. |

## `durable_function`

| file | function | purpose |
|---|---|---|
| `temporal_backend.py` | `class RoleRequest` | Everything an activity needs. Activities must not close over workflow-local state. |
| `temporal_backend.py` | `class PipelineRequest` |  |
| `temporal_backend.py` | `async run_role(req)` | One stage = one activity attempt. Retried by Temporal, never run twice concurrently. |
| `temporal_backend.py` | `async run_command(cmd, cwd, timeout_s)` | Run a shell command the factory needs (tests, exporters, acceptance checks). |
| `temporal_backend.py` | `class DeliveryPipelineWorkflow` | research -> plan -> (implement -> review -> verify -> gates) x max_rounds. |
| `temporal_backend.py` | `class SmokeWorkflow` | Two commands back to back. Proves worker + activities + history with no model spend. |
| `temporal_backend.py` | `async connect` |  |
| `temporal_backend.py` | `async start_worker(task_queue, client)` | Create the worker and run it as a background task on the CURRENT loop (the kernel's). |
| `temporal_backend.py` | `async start_run(brief, run_dir, *, stage, task_kind, max_rounds, gates, route, workflow_id, client)` | Write task.md, then start the workflow. Returns (handle, run_dir). |
| `temporal_backend.py` | `async probe_child(prompt, model)` | Spawn one trivial child through the SAME path the real roles use (_run_role -> rlm.spawn). |
| `temporal_backend.py` | `class ProbeWorkflow` |  |
| `temporal_backend.py` | `class RustProbeWorkflow` | Cross-language routing check: this workflow executes on the Python worker while its activity is |
| `temporal_backend.py` | `class RustToolsWorkflow` | Drives the production Rust worker: inspect an exported trajectory (pure Rust), then run the |
| `temporal_backend.py` | `class RustCommandWorkflow` | Run one mechanical command through the Rust worker (e.g. an acceptance command from a spec). |

## `factory`

| file | function | purpose |
|---|---|---|
| `engine.py` | `load_config` | load pipeline.json (hooks, models, stages) |
| `engine.py` | `async fire(event, ctx, cfg, log)` |  |
| `engine.py` | `async run_stages(task_brief, *, stage, work_dir, max_rounds, keep_children, cfg, model, reviewer_model, gate_model, linear_id, task_kind, route_models)` | Run the staged engine. `stage` is one of dev, test, staging, prod. |
| `engine.py` | `async run_stages_task(brief_file)` |  |
| `engine.py` | `async run_role_stage(role_id, *, run_dir, values, task_kind, model, models, thinking, route, task_brief, exclude, name, on_spawn)` | Render ONE role's prompt and run it as a child agent. Single source of truth, shared by |
| `engine.py` | `async stop_child(name)` | Stop a child agent: tell it to stop, then try to remove it. |
| `export_machine.py` | `build` | render the machine spec from roles.py |
| `export_machine.py` | `main` | write MACHINE.md for the staged-delivery machine |
| `export_machine.py` | `states_of(machine)` | list the machine's state ids |
| `machine_runner.py` | `async run_stages(task_brief, *, work_dir, max_rounds, keep_children, model, reviewer_model)` | Run the machine on one task brief. Returns a summary dict. |
| `machine_runner.py` | `async run_stages_task(brief_file)` | Run from a brief file (default .prime-factory/task.md). |
| `routing.py` | `batch_gate(*, single_turn, blocking, latency_ok, items, savings_matter)` | Strict precondition check. Returns (allowed, [failed conditions]). All five must hold. |
| `routing.py` | `async route(role, task, *, pool, exclude, threshold, require_confident, allow_batch, batch_context, state, cfg)` | Pick a model from the pool. |
| `routing.py` | `async route_or_default(role, task, *, default, allow_batch)` | Route, and fall back to the deterministic default when the decision escalates. |

## `decision_layer`

| file | function | purpose |
|---|---|---|
| `decision.py` | `choice(instructions, options)` | Choice question. options: ["a","b"] or [("a","why a"), ...] -> the documented Criteria map. |
| `decision.py` | `score(instructions, levels)` | Score question. levels: ordered low -> high, 2..10 levels. |
| `decision.py` | `noul(instructions, true_desc, false_desc)` | Noul question (yes/no). criteria is optional and only sharpens the boundary. |
| `decision.py` | `async ask(state, questions, *, model, cfg, timeout)` | Ask one or more questions (choice/score/noul mixed) against one state. Returns the answers map. |
| `decision.py` | `async decide(question, options, *, context, threshold, model, state, mode, cfg)` | Choice primitive + escalation rule: auto-take a choice above the bar, else escalate. |
| `decision.py` | `async ask_or_decide(question, options, *, context, threshold)` |  |
| `decision_mcp.py` | `async ask(state, questions)` | Ask one or more questions (choice/score/noul mixed) against one state. |
| `decision_mcp.py` | `async decide(question, options, context, threshold)` | Choice + escalation: auto-take the option above `threshold`, else set escalate=True. |
| `decision_mcp.py` | `async ask_or_decide(question, options, context, threshold)` | Like `decide`, but an escalation carries `ask_user` text to show the human. |
| `decision_mcp.py` | `async score(instructions, levels, state)` | Rate a state on a rubric: levels ordered low -> high (2..10 entries). Returns score/probabilitie |
| `decision_mcp.py` | `async noul(instructions, state, true_desc, false_desc)` | Judge a yes/no statement against a state. Returns noul in 0..1 (1 = yes). |
| `decision_mcp.py` | `decision_status` | Decision model, escalation rule, threshold and the configured fallback chain. |
| `decision_mcp.py` | `main` | MCP server entry point |

## `continual_learning`

| file | function | purpose |
|---|---|---|
| `reflect.py` | `gather(day, *, max_runs)` | Collect the day's raw material as one prompt per item (custom_id -> prompt). |
| `reflect.py` | `submit(day, *, model, items)` | Submit one batch job for the day. Returns {"day","id","items","status", "mode"}. |
| `reflect.py` | `collect(day, *, write, dry_run)` | Fetch a submitted reflection and write the vault note (+ proposals). |
| `reflect.py` | `tick(now, *, submit_hour, collect_after_h, verbose)` | One scheduler pass. Pure Python - no model turn, no tokens. |
| `reflect.py` | `await_sync(module, prompt)` | One synchronous synthesis call (a chat model, not batch). |

## `llm_wiki`

| file | function | purpose |
|---|---|---|
| `obsidian_sync.py` | `load_state(path)` | read the previous sync state |
| `obsidian_sync.py` | `render_entry(entry, all_ids)` | render one harness entry as markdown |
| `obsidian_sync.py` | `export_machines(vault, dry_run)` | Library machines (~/.prime/agent/machines/<name>/MACHINE.md) into Prime Agent/Machines/. |
| `obsidian_sync.py` | `sync(vault, dry_run, quiet)` |  |
| `obsidian_sync.py` | `export_runs(vault, runs_root, dry_run, quiet)` | One folder per factory run under Projects/<project>/Plans/<YYYY>/<MM>/<DD>/<tag>/. |
| `obsidian_sync.py` | `export_practices(vault, dry_run)` | One root folder per recurring topic, each an index over harness entries + plans. |
| `obsidian_sync.py` | `sync_all(vault, dry_run, quiet)` | Harness mirror + project plans + practice roots. |

## `batch`

| file | function | purpose |
|---|---|---|
| `batch.py` | `submit(prompts, *, model, system, endpoint, max_tokens, temperature)` | Submit one batch job. `prompts` maps custom_id -> prompt text (or a full messages list). |
| `batch.py` | `status(batch_id)` | poll a batch job |
| `batch.py` | `wait(batch_id, *, timeout_s, poll_s, verbose)` | Poll until a terminal status or the timeout. Returns the final status record. |
| `batch.py` | `results(batch_id)` | Fetch results: custom_id -> answer text (or {'error': ...}). |
| `batch.py` | `run(prompts, *, model, timeout_s, poll_s, verbose)` | Submit, wait, fetch. Returns {"answers": {...}, "batch": <final status>}. |

## `tools`

| file | function | purpose |
|---|---|---|
| `model_cache_refresh.py` | `cached_ids(provider)` | Model ids the prime-agent catalog already knows for a provider. |
| `model_cache_refresh.py` | `to_definition(model)` | OpenRouter model entry -> prime-agent ModelDefinition. |
| `model_cache_refresh.py` | `refresh_models(provider, *, url, all_models, dry_run, verbose, extras)` | Refresh the local override from the provider API. Returns a summary dict. |
| `model_econ.py` | `norm(s)` | Normalise a model name for matching: lowercase alphanumerics only. |
| `model_econ.py` | `best_aa_name(or_name, table)` | AA short names ('MiMo-V2.6-Pro') vs OpenRouter names ('Xiaomi: MiMo-V2.6-Pro'). |
| `model_econ.py` | `rsc_payload(html)` | Decode a Next.js RSC payload: the benchmark JSON is inside escaped JS strings. |
| `model_econ.py` | `aa_data` | model name -> {"<bench>": {"score","out_tokens","cost"}} and name -> speed (tok/s). |
| `model_econ.py` | `or_prices` | live OpenRouter prices |
| `model_econ.py` | `or_throughput(model_id)` | Average of the providers' throughput_last_30m (OpenRouter's own number), or None. |
| `model_econ.py` | `measured_speed` | tokens/s observed in our own runs, from role spans that recorded duration_ms. |
| `model_econ.py` | `main` | CLI entry point for the cost/speed report |
