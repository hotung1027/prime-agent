# factory - multi-agent orchestration

The machine, the roles, and the staged engine. No durability, no tracing: those are separate plugins.

- `roles.py` - the twelve role prompts and the answer key each one must return. Single source of truth.
- `export_machine.py` - generates `machines/staged-delivery/MACHINE.md` from `roles.py`.
- `machine_runner.py` - deterministic runner for a factory machine: waits until a child's session file
  contains the answer it needs instead of trusting preview capture.
- `engine.py` - the staged engine: roles, hooks, per-role model routing, stage gates, rounds.
- `engine.json` - the engine's configuration: hook events, model tiers, doc stages (no secrets).
- `routing.py` - per-role model choice from a curated pool, plus the batch gate.

## The machine

    research -> plan -> implement -> review -> verify -> merge
                                   ^          |
                                   +----------+   (failed review or verify loops back)

    python export_machine.py                 # regenerate MACHINE.md from roles.py
    r = await rlm.factory.run("staged-delivery")
    await rlm.factory.status(r["run_id"])

The machine spec is committed because it is the reviewed artifact; `roles.py` is what you edit.

## Two runners, one definition

`engine.py` runs the stages in-process with hooks and gates. `machine_runner.py` runs the machine
and reads each answer from the child's session file. `durable_function` wraps `engine.py` in Temporal.
All three consume the same `roles.py`, so the role prompts never fork.
