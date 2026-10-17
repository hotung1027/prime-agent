# pipeline-workers

Rust worker for the prime-agent delivery engine. Serves the **mechanical** activities (run the
Julia test suite, run the trajectory exporter, inspect exported trajectories, run a command with a
timeout). Activities that spawn child agents stay in the Python kernel worker, because they need
`rlm`; Temporal routes each activity to a task queue, so one workflow can use both workers.

## Build

The Temporal SDK's macro output refers to `::futures` and `::temporalio_common` as absolute paths, so
both are direct dependencies in `Cargo.toml` - do not remove them. The build also needs `protoc`:

```bash
# no sudo needed on this machine:
#   download protoc-<ver>-linux-x86_64.zip from the protobuf GitHub releases and extract it
PROTOC=$HOME/.cache/protoc/bin/protoc cargo build --release
```

## Run

```bash
TEMPORAL_ADDRESS=localhost:7233 TEMPORAL_NAMESPACE=default \
  ./target/release/pipeline-workers rust-tools
```

Activity names are pinned with `#[activity(name = "...")]` on purpose: the SDK's default name is
`"<Struct>::<method>"`, which a Python (or Go, or Java) caller will never guess.

| activity | input | output |
|---|---|---|
| `run_command` | `CommandRequest{command,args,cwd,timeout_s,env}` | `CommandResult{exit_code,duration_ms,timed_out,output_tail}` |
| `run_tests` | `RunTestsRequest{repo,project,timeout_s,test_file?}` | `CommandResult` |
| `export_trajectories` | `ExportRequest{repo,project,script?,files[],timeout_s}` | `ExportResult{export,trajectories[]}` |
| `inspect_trajectory` | `path: String` | `TrajectorySummary{...}` |

## Scope

The activities here are examples shaped for one artefact schema (`target`, `direction`,
`ori_error`, `samples`). Replace them with the schema your project emits; `run_command` is generic.
