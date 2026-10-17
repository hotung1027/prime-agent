# Verified versions

Measured on the development machine (WSL2, Linux) on 2026-10-15.

| component | version | notes |
|---|---|---|
| Python | 3.11.16 | prime-agent kernel venv |
| temporalio (Python) | 1.34.0 | **the durable layer** - workflow, activities, signals |
| opentelemetry-api / -sdk / -proto | 1.45.1 | tracing |
| opentelemetry-exporter-otlp-proto-http | 1.45.1 | tracing |
| Julia | 1.13.1 | machine tooling + view |
| rustc / cargo | 1.98.0 | optional Rust **activity** worker only (not the workflow) |
| temporalio-sdk / -client / -macros | 1.1.0 | optional Rust **activity** worker only |
| temporalio-sdk-core | 0.10.0 | optional Rust activity worker only |

No `langfuse` Python SDK is used: tracing speaks OTLP directly.

## Runtime compatibility

| item | value |
|---|---|
| prime-agent install | 0.10.0 (`install-rust.sh channel stable`) |
| repo `main` at the base of this branch | `7cbffa77b` |
| runtime files that differ between the 0.10.0 build and `main` | `__init__.py`, `factory.py`, `harness.py` |

Check that difference before relying on a factory API: this version targets the 0.10.0 surface.
