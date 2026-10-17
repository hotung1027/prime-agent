
"""MCP server for the TypeSafe decision layer (choice / score / noul), following docs.typesafe.ai.

Tools mirror the three documented primitives; `ask` mixes any number of them in one call against one
state. Registered with:
    prime-agent mcp add decision -- <kernel python> <this file>
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.home() / ".prime" / "agent" / "pipelines"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from mcp.server.mcpserver import MCPServer  # noqa: E402

import decision as decision_layer  # noqa: E402

server = MCPServer(
    "decision-layer",
    instructions=("TypeSafe System One decisions (jev). Three primitives, mixable in one call against "
                  "one state: choice (pick from a list -> choice/probabilities/confidence), score "
                  "(rate on a rubric -> score/probabilities/confidence), noul (yes/no -> 0..1). "
                  "Use `ask` for one or many mixed questions, `decide` when a single choice should be "
                  "escalated to a human above a confidence threshold."),
)


@server.tool()
async def ask(state: dict | list | str, questions: dict) -> dict:
    """Ask one or more questions (choice/score/noul mixed) against one state.

    questions: {"<id>": {"type":"choice","instructions":"...","criteria":{"opt":"why"}} |
                        {"type":"score","instructions":"...","criteria":["low","mid","high"]} |
                        {"type":"noul","instructions":"...","criteria":{"true":"...","false":"..."}}}
    """
    return await decision_layer.ask(state, questions)


@server.tool()
async def decide(question: str, options: list, context: str = "", threshold: float = 0.9) -> dict:
    """Choice + escalation: auto-take the option above `threshold`, else set escalate=True."""
    return await decision_layer.decide(question, options, context=context, threshold=threshold)


@server.tool()
async def ask_or_decide(question: str, options: list, context: str = "", threshold: float = 0.9) -> dict:
    """Like `decide`, but an escalation carries `ask_user` text to show the human."""
    return await decision_layer.ask_or_decide(question, options, context=context, threshold=threshold)


@server.tool()
async def score(instructions: str, levels: list, state: dict | list | str) -> dict:
    """Rate a state on a rubric: levels ordered low -> high (2..10 entries). Returns score/probabilities."""
    return await decision_layer.ask(state, {"score": decision_layer.score(instructions, levels)})


@server.tool()
async def noul(instructions: str, state: dict | list | str,
               true_desc: str = "", false_desc: str = "") -> dict:
    """Judge a yes/no statement against a state. Returns noul in 0..1 (1 = yes)."""
    return await decision_layer.ask(
        state, {"noul": decision_layer.noul(instructions, true_desc or None, false_desc or None)})


@server.tool()
def decision_status() -> dict:
    """Decision model, escalation rule, threshold and the configured fallback chain."""
    import core
    cfg = core.load_settings()
    return {"models": core.models_for("decision", cfg),
            "rule": cfg.get("decision_rule", decision_layer.RULE),
            "threshold": decision_layer.THRESHOLD,
            "types": ["choice", "score", "noul"]}


def main():
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
