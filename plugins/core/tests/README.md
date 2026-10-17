# core tests

`test_bus.py` - the event bus contract, no model and no Temporal needed:

    python tests/test_bus.py

Covers: inline verdicts, the mandatory matcher for tool events, the depth guard, content dedupe,
subprocess delivery (allowlist + sha256 pinning), durable delivery (transport present and absent),
and continuations re-entering the bus.
