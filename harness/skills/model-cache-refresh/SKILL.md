---
name: model-cache-refresh
description: Model catalog as a cache: refresh prime-agent's model list from the provider API
---

Treat prime-agent's model catalog as a CACHE and refresh it from the provider API.

    import model_cache_refresh as mcr
    mcr.refresh_models()            # GET https://openrouter.ai/api/v1/models, add what is missing
    mcr.refresh_models(dry_run=True)  # report only
Why: prime-agent resolves models from a fetched catalog snapshot and REFUSES any model absent from
it, so a model OpenRouter already serves can be unusable. This writes the missing models into the
official override ~/.prime/agent/models.json, which the daemon re-reads live (no restart).
Schema it writes: {"providers": {"openrouter": {"models": [ModelDefinition, ...]}}} where a
ModelDefinition is {"id","name","api":"openai-completions","reasoning","input",
"cost":{"input","output","cacheRead","cacheWrite"} (USD per Mtok),"contextWindow","maxTokens"}.
CLI: python3 $HOME/.prime/agent/pipelines/model_cache_refresh.py [--dry-run|--all].
Verify with `prime-agent model list <name>`; errors surface as "Warning: errors loading models.json".
On this account OpenAI/Google/Anthropic return 403; x-ai, mistralai, moonshotai(excluded), qwen,
deepseek answer. A background loop refreshes this every 10 minutes.
