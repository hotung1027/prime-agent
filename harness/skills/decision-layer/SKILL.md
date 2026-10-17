---
name: decision-layer
description: Decision layer: TypeSafe primitives (choice/score/noul) + escalation above 0.9
---

Route options and judgments through TypeSafe's System One model (jev) before asking the user.

Three documented primitives (docs.typesafe.ai/primitives), all evaluated in parallel and in isolation
against ONE `state`, mixable in a single call. `state` is a string, object or array; you choose the
question ids (the model never sees them).

    import decision
    questions = {
      "approach": decision.choice("Which approach?", [("barrier","soft penalty"),
                                                       ("projected step","project the DLS step")]),
      "severity": decision.score("How severe?", ["cosmetic","degraded","blocking"]),   # 2..10 levels
      "needs_human": decision.noul("Does this need human review?",
                                   "touches safety geometry", "isolated helper"),        # criteria optional
    }
    reply = await decision.ask({"task": "...", "defect": "..."}, questions)
    reply["answers"]["approach"]   # {type, choice, probabilities, confidence}
    reply["answers"]["severity"]   # {type, score, legend:{level:text}, probabilities, confidence}
    reply["answers"]["needs_human"]# {type, noul}   # 0..1, 1 = yes

Single choice with escalation:
    res = await decision.decide(question, [("opt","why"), ...], context="...")   # or ask_or_decide
    res["escalate"], res["choice"], res["confidence"], res["option_confidence"], res["ranked"]
Auto-takes the choice when it clears `threshold` (0.9) under `models.decision_rule` ("both" |
"option" | "model"); otherwise the CALLER must ask the user and show res["ranked"].

Costs ~$0.00002 per call. jev is a decisions model: it CANNOT be used with /chat/completions, and it
is absent from /api/v1/models (register via the endpoints probe in model_cache_refresh). A plain chat
model works as a fallback decision model. MCP: the same three primitives are exposed by the `decision`
stdio server (tools: ask, decide, ask_or_decide, score, noul, decision_status).
