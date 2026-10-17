
"""Treat prime-agent's model catalog as a CACHE, refreshed from the provider API.

prime-agent resolves models from a catalog snapshot and refuses any model absent from it.
This tool pulls the live OpenRouter list (`/api/v1/models`), diffs it against that snapshot,
and writes the missing models into the official override `~/.prime/agent/models.json`, which
prime-agent re-reads live (no daemon restart needed).

    python model_cache_refresh.py            # refresh the cache
    python model_cache_refresh.py --dry-run  # report only
    python model_cache_refresh.py --all      # rewrite every live model, not just the missing ones

Call `refresh_models()` from Python for the same thing (used by the pipeline engine).
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

AGENT_DIR = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR")
                 or Path.home() / ".prime" / "agent")
CATALOG = AGENT_DIR / "models" / "provider-model-catalog.v1.json"
OVERRIDE = AGENT_DIR / "models.json"
OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
# Models that OpenRouter serves but does NOT list in /api/v1/models (verified: typesafe/jev-1.13 is
# reachable only through /api/v1/models/<id>/endpoints). Probe these explicitly so they stay known.
EXTRA_MODEL_IDS = ["typesafe/jev-1.13", "typesafe/jev-router"]
TIMEOUT = 30


def _get_json(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cached_ids(provider="openrouter"):
    """Model ids the prime-agent catalog already knows for a provider."""
    if not CATALOG.exists():
        return set()
    payload = json.loads(CATALOG.read_text()).get("payload", {})
    return {m["id"] for m in payload.get("models", []) if m.get("provider") == provider}


def _per_mtok(value):
    try:
        return round(float(value or 0) * 1_000_000, 6)
    except (TypeError, ValueError):
        return 0


def to_definition(model):
    """OpenRouter model entry -> prime-agent ModelDefinition."""
    arch = model.get("architecture") or {}
    pricing = model.get("pricing") or {}
    top = model.get("top_provider") or {}
    params = model.get("supported_parameters") or []
    inputs = [m for m in (arch.get("input_modalities") or ["text"]) if m in ("text", "image", "audio", "video")]
    context = model.get("context_length") or top.get("context_length") or 0
    definition = {
        "id": model["id"],
        "name": model.get("name") or model["id"],
        "api": "openai-completions",
        "reasoning": bool({"reasoning", "reasoning_effort", "include_reasoning"} & set(params)),
        "input": inputs or ["text"],
        "cost": {
            "input": _per_mtok(pricing.get("prompt")),
            "output": _per_mtok(pricing.get("completion")),
            "cacheRead": _per_mtok(pricing.get("input_cache_read")),
            "cacheWrite": _per_mtok(pricing.get("input_cache_write")),
        },
        "contextWindow": int(context),
        "maxTokens": int(top.get("max_completion_tokens") or context or 0),
    }
    return definition


def _probe_extra(model_id, provider="openrouter"):
    """Fetch one model's metadata from the endpoints API; None when it does not exist."""
    url = f"https://openrouter.ai/api/v1/models/{model_id}/endpoints"
    try:
        data = _get_json(url).get("data") or {}
    except Exception:
        return None
    endpoint = (data.get("endpoints") or [{}])[0]
    pricing = endpoint.get("pricing") or {}
    if not endpoint.get("context_length"):
        return None            # metadata-less endpoint: registering it would be invalid
    arch = data.get("architecture") or {}
    return {
        "id": model_id,
        "name": data.get("name") or model_id,
        "api": "openai-completions",
        "reasoning": False,
        "input": [m for m in (arch.get("input_modalities") or ["text"]) if m in ("text", "image")] or ["text"],
        "cost": {"input": _per_mtok(pricing.get("prompt")), "output": _per_mtok(pricing.get("completion")),
                 "cacheRead": _per_mtok(pricing.get("input_cache_read")), "cacheWrite": 0},
        "contextWindow": int(endpoint.get("context_length") or 0),
        "maxTokens": int(endpoint.get("max_completion_tokens") or 0),
    }


def refresh_models(provider="openrouter", *, url=OPENROUTER_URL, all_models=False,
                   dry_run=False, verbose=True, extras=True):
    """Refresh the local override from the provider API. Returns a summary dict."""
    try:
        live = _get_json(url).get("data", [])
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "added": 0}
    known = set() if all_models else cached_ids(provider)
    # ":batch" variants are async batch-API models: they 404 on /chat/completions, so never register
    fresh = [to_definition(m) for m in live
             if m["id"] not in known and m["id"] != provider and not m["id"].endswith(":batch")]
    if extras:
        for model_id in EXTRA_MODEL_IDS:
            extra = _probe_extra(model_id, provider)
            if extra:
                fresh.append(extra)

    override = json.loads(OVERRIDE.read_text()) if OVERRIDE.exists() else {}
    providers = override.setdefault("providers", {})
    entry = providers.setdefault(provider, {})
    existing = {m["id"]: m for m in entry.get("models", [])}
    changed = 0
    for definition in fresh:
        if existing.get(definition["id"]) != definition:
            existing[definition["id"]] = definition
            changed += 1
    entry["models"] = [existing[k] for k in sorted(existing)]
    if not dry_run and changed:
        OVERRIDE.write_text(json.dumps(override, indent=2) + "\n")
    summary = {"ok": True, "provider": provider, "live": len(live), "cached": len(known),
               "added_or_updated": changed, "override_total": len(entry["models"]),
               "path": str(OVERRIDE), "dry_run": dry_run}
    if verbose:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    import sys
    refresh_models(all_models="--all" in sys.argv, dry_run="--dry-run" in sys.argv)
