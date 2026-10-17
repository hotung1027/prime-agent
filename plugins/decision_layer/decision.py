
"""Decision layer on TypeSafe's System One model (jev) - follows the documented primitives.

TypeSafe exposes three question types (docs.typesafe.ai/primitives), all evaluated in parallel and
in isolation against one `state`, mixable in a single request:

  Choice  {"type":"choice","instructions":...,"criteria":{"<option>":"<description>", ...}}
          -> {choice, probabilities, confidence}
  Score   {"type":"score","instructions":...,"criteria":["<low level>", ..., "<high level>"]}
          -> {score, probabilities, confidence}      (2..10 levels)
  Noul    {"type":"noul","instructions":"<yes/no question or statement>",
           "criteria":{"true":"...","false":"..."}}  # criteria optional
          -> {noul: 0..1}

Question ids are yours; the model never sees them. `state` is a string, object or array.

    import decision
    q = {"storage": decision.choice("Which storage backend?",
                                    [("SQLite","embedded, zero config"),
                                     ("Postgres","server process")])}
    ans = await decision.ask({"context": "offline desktop app"}, q)
    ans["storage"]["choice"], ans["storage"]["confidence"], ans["storage"]["probabilities"]

    res = await decision.decide("Which storage backend?", [("SQLite","..."), ...], context="...")
    # decide() is the choice primitive + the escalation rule: auto-take above `threshold`,
    # otherwise return escalate=True and the ranked options for the human.

Transport: POST https://openrouter.ai/api/alpha/decisions with the OpenRouter key from
~/.prime/agent/auth.json (or OPENROUTER_API_KEY). jev cannot use /chat/completions. A plain chat
model can substitute as the decision model (chat fallback), and every call is traced.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import core
import tracing

THRESHOLD = 0.9
# "both" (default) = the winning option's probability AND the model's own confidence must exceed the
# threshold; "option" = only the option probability; "model" = only the model confidence.
RULE = "both"
API = "https://openrouter.ai/api/alpha/decisions"
AUTH = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR") or Path.home() / ".prime" / "agent") / "auth.json"

CHAT_PROMPT = """You are a decision layer. Choose the single best option, or state that you cannot.

Question: {question}
Context: {context}

Options:
{options}

Be calibrated, not confident: if two options are close, give them similar probabilities.

Reply with ONE json object and nothing else:
{{"confidence": {{"<exact option text>": <0.0-1.0>, ...}}, "choice": "<exact option text>", "reason": "<one line>"}}"""


# ------------------------------------------------------------------ question builders

def choice(instructions, options):
    """Choice question. options: ["a","b"] or [("a","why a"), ...] -> the documented Criteria map."""
    criteria = {}
    for option in options:
        if isinstance(option, (tuple, list)) and len(option) == 2:
            criteria[str(option[0])] = str(option[1])
        else:
            criteria[str(option)] = str(option)
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions, levels):
    """Score question. levels: ordered low -> high, 2..10 levels."""
    return {"type": "score", "instructions": instructions, "criteria": [str(l) for l in levels]}


def noul(instructions, true_desc=None, false_desc=None):
    """Noul question (yes/no). criteria is optional and only sharpens the boundary."""
    question = {"type": "noul", "instructions": instructions}
    if true_desc or false_desc:
        question["criteria"] = {"true": true_desc or "yes", "false": false_desc or "no"}
    return question


# ------------------------------------------------------------------ transport

def _key():
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    if AUTH.exists():
        data = json.loads(AUTH.read_text())
        entry = data.get("openrouter")
        if isinstance(entry, dict):
            return entry.get("key") or entry.get("apiKey") or entry.get("token")
        if isinstance(entry, str):
            return entry
    raise RuntimeError("no OpenRouter key: set OPENROUTER_API_KEY or configure /login")


def _post(payload, timeout=120):
    request = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key()}"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="ignore")[:400]
        raise RuntimeError(f"decisions API HTTP {exc.code}: {detail}") from None


def _api_model(model):
    return model.split("/", 1)[1] if model and model.startswith("openrouter/") else model


async def ask(state, questions, *, model=None, cfg=None, timeout=120):
    """Ask one or more questions (choice/score/noul mixed) against one state. Returns the answers map."""
    cfg = cfg or core.load_settings()
    chain = [model] if model else core.models_for("decision", cfg)
    api_model = _api_model(chain[0])
    payload = {"model": api_model,
               "state": state if isinstance(state, (dict, list, str)) else {"state": state},
               "questions": questions}
    with tracing.span("decision-ask", questions=list(questions), model=api_model) as span:
        _, reply = _post(payload, timeout=timeout)
        answers = reply.get("answers") or {}
        cost = (reply.get("usage") or {}).get("cost")
        span.attr(cost_usd=cost, answer_types={k: v.get("type") for k, v in answers.items()})
    tracing.event("decision-answers", questions=list(questions), cost_usd=cost,
                  summary={k: {kk: vv for kk, vv in v.items() if kk != "probabilities"}
                           for k, v in answers.items()})
    tracing.flush()
    return {"answers": answers, "model": reply.get("model"), "cost_usd": cost, "api": "alpha-decisions"}


# ------------------------------------------------------------------ choice + escalation

def _escalates(option_confidence, model_confidence, threshold, rule=None):
    rule = (rule or os.environ.get("PRIME_AGENT_DECISION_RULE") or RULE).lower()
    if rule == "option":
        return option_confidence <= threshold
    if rule == "model":
        return model_confidence <= threshold
    return not (option_confidence > threshold and model_confidence > threshold)


async def decide(question, options, *, context="", threshold=THRESHOLD, model=None, state=None,
                 mode="alpha", cfg=None):
    """Choice primitive + escalation rule: auto-take a choice above the bar, else escalate."""
    options = [(str(o[0]), str(o[1])) if isinstance(o, (tuple, list)) and len(o) == 2 else (str(o), str(o))
               for o in options]
    if len(options) == 1:
        return {"escalate": False, "choice": options[0][0], "confidence": 1.0, "ranked": options[:1],
                "reason": "single option", "api": "trivial", "model": None, "cost_usd": 0.0}
    cfg = cfg or core.load_settings()
    chain = [model] if model else core.models_for("decision", cfg)
    rule = cfg.get("decision_rule") or RULE
    api_state = {"context": context, **(state or {})}
    if mode == "alpha" and chain and "jev" in (chain[0] or ""):
        reply = await ask(api_state, {"decision": choice(question, options)}, model=chain[0], cfg=cfg)
        answer = reply["answers"].get("decision") or {}
        probabilities = {str(k): float(v) for k, v in (answer.get("probabilities") or {}).items()}
        ranked = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
        picked = str(answer.get("choice") or (ranked[0][0] if ranked else ""))
        option_conf = float(probabilities.get(picked, 0.0))
        model_conf = float(answer.get("confidence") or option_conf)
        return {"escalate": _escalates(option_conf, model_conf, threshold, rule), "choice": picked,
                "confidence": model_conf, "option_confidence": option_conf, "ranked": ranked,
                "reason": "", "model": reply["model"], "cost_usd": reply["cost_usd"],
                "api": "alpha-decisions", "rule": rule, "threshold": threshold}
    return await _decide_chat(question, options, context, chain, threshold, cfg)


async def _decide_chat(question, options, context, chain, threshold, cfg):
    prompt = CHAT_PROMPT.format(question=question, context=context or "(none)",
                                options="\n".join(f"{i+1}. {lab} - {desc}"
                                                  for i, (lab, desc) in enumerate(options)))
    handle, parsed, used = await core.run_child("decision-chat", prompt, "confidence",
                                                    models=chain, timeout=600)
    confidences = {str(k): float(v) for k, v in (parsed.get("confidence") or {}).items()}
    ranked = sorted(confidences.items(), key=lambda kv: kv[1], reverse=True)
    picked = str(parsed.get("choice") or (ranked[0][0] if ranked else ""))
    confidence = float(confidences.get(picked, ranked[0][1] if ranked else 0.0))
    return {"escalate": _escalates(confidence, confidence, threshold), "choice": picked,
            "confidence": confidence, "option_confidence": confidence, "ranked": ranked,
            "reason": str(parsed.get("reason", "")), "model": used, "cost_usd": None,
            "api": "chat", "rule": "n/a", "threshold": threshold}


async def ask_or_decide(question, options, *, context="", threshold=THRESHOLD, **kw):
    result = await decide(question, options, context=context, threshold=threshold, **kw)
    if result["escalate"]:
        result["ask_user"] = (f"Decision layer escalated (best confidence {result['confidence']:.2f} "
                              f"<= {threshold}).\n{question}\n"
                              + "\n".join(f"  - {o}  ({c:.2f})" for o, c in result["ranked"]))
    return result
