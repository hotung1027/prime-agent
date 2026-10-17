# durable_function - durable execution (optional)

Provides durable runs, retries, history and a UI **on top of the factory's staged engine**. This plugin
owns no orchestration of its own: it wraps `factory/engine.py`.

- `temporal_backend.py` - the Temporal workflow/activity layer: one activity per stage, retries,
  `from_stage` resume, gates-only runs, an activity that spawns children and takes them down on
  cancellation.
- `worker-rs/` - the Rust worker for mechanical activities (`run_command`, `run_tests`,
  `export_trajectories`, `inspect_trajectory`).

Required: `TEMPORAL_ADDRESS`, `TEMPORAL_NAMESPACE`. Build the worker with
`PROTOC=<path> cargo build --release` (the Temporal SDK macros need `protoc`).

## SDK split (important)

- **The durable function is Python**: `temporal_backend.py` uses the Python SDK (`temporalio` 1.34.0)
  inside the kernel worker - the workflow, the activities, the retries, the signals and the
  cancellation handling are all Python.
- **`worker-rs/` is Rust, and only for mechanical activities**: a separate worker on the `rust-tools`
  task queue that executes `run_command` / `run_tests` / `export_*` / `inspect_*`. It runs no
  workflows and is not required for the durable layer to work.

This plugin is **optional**: `rlm.factory` already provides durable runs, pause/resume and stop. It is
only useful when you want Temporal's own history, retries and UI.
