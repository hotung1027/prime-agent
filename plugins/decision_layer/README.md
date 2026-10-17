# decision_layer - structured choices

`decision.py` routes selections and judgments through a small decision model before a human is asked:
`choice` (pick one option), `score` (a number + confidence), `noul` (no preference). Above a
confidence threshold the answer is taken; below it the caller escalates.

`decision_mcp.py` exposes the same primitives as an MCP server (`ask`, `decide`, `ask_or_decide`,
`score`, `noul`, `decision_status`) so any MCP client can use them.

    import decision
    pick = await decision.choice(state, {"model": decision.choice(["a", "b"], "which?")})

Dependency: spawns its judge child through `engine._run_role` (durable_function). That is a private
helper - a follow-up should route it through the factory.
