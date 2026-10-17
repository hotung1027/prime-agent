
"""OpenRouter batch API - cheap, non-streaming, one-shot calls for the factory.

Batch models (`<model>:batch`) are ~50-62% cheaper but are served ONLY through OpenRouter's async
batch API: a chat/completions call with a `:batch` model returns
    404 "... cannot be used with the chat/completions endpoint (adapter DeepInfraBatchAdapter)".
This module implements that API, so a role - or any bulk one-shot work - can use batch pricing.

    import batch
    job = batch.submit({"one": "Summarise: ...", "two": "Classify: ..."},
                       model="deepseek/deepseek-v4.1-flash:batch")
    done = batch.wait(job["id"], timeout_s=1800)      # polls the batch to a terminal state
    answers = batch.results(job["id"])                 # {"one": "text", "two": "text"}

WHAT IT IS NOT: a drop-in model for agentic children. Batch requests are single-turn and
non-streaming, so a role that must call ipython/bash across many turns cannot run this way. Use it
for single-prompt work: summarise, classify, extract, judge an artifact, draft a short text.
Cost is the same as the batch model's discounted price; completion_window is 24h, so latency is
minutes to hours, not seconds.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://openrouter.ai/api/v1/batches"
AUTH = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR") or Path.home() / ".prime" / "agent") / "auth.json"
TERMINAL = {"completed", "failed", "cancelled", "expired"}


def _key():
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    entry = json.loads(AUTH.read_text()).get("openrouter")
    if isinstance(entry, dict):
        return entry.get("key") or entry.get("apiKey") or entry.get("token")
    return entry


def _call(method, url, payload=None, timeout=60, raw=False):
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key()}"},
        method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode(errors="ignore")
            return response.status, (body if raw else json.loads(body))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"batch API HTTP {exc.code}: {exc.read()[:300].decode(errors='ignore')}") from None


def submit(prompts, *, model, system=None, endpoint="/v1/chat/completions", max_tokens=None,
           temperature=None):
    """Submit one batch job. `prompts` maps custom_id -> prompt text (or a full messages list)."""
    requests = []
    for custom_id, prompt in prompts.items():
        messages = prompt if isinstance(prompt, list) else (
            ([{"role": "system", "content": system}] if system else []) +
            [{"role": "user", "content": str(prompt)}])
        body = {"messages": messages}
        if max_tokens:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        requests.append({"custom_id": str(custom_id), "body": body})
    payload = {"endpoint": endpoint, "model": model, "requests": requests}
    status, reply = _call("POST", API, payload)
    return {"id": reply.get("id"), "status": reply.get("status"), "model": reply.get("model"),
            "count": len(requests), "raw": reply}


def status(batch_id):
    _, reply = _call("GET", f"{API}/{batch_id}")
    return reply


def wait(batch_id, *, timeout_s=1800, poll_s=15, verbose=False):
    """Poll until a terminal status or the timeout. Returns the final status record."""
    started = time.time()
    while True:
        rec = status(batch_id)
        state = rec.get("status")
        if verbose:
            counts = rec.get("request_counts") or {}
            print(f"  [{int(time.time()-started):4d}s] {state} {counts}")
        if state in TERMINAL:
            return rec
        if time.time() - started > timeout_s:
            rec["timed_out"] = True
            return rec
        time.sleep(poll_s)


def results(batch_id):
    """Fetch results: custom_id -> answer text (or {'error': ...})."""
    _, body = _call("GET", f"{API}/{batch_id}/results", raw=True)
    out = {}
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        custom_id = rec.get("custom_id")
        resp = rec.get("response") or {}
        choices = (resp.get("body") or {}).get("choices") or []
        if choices:
            out[custom_id] = (choices[0].get("message") or {}).get("content")
        else:
            out[custom_id] = {"error": rec.get("error") or resp.get("status_code") or "no content"}
    return out


def run(prompts, *, model, timeout_s=1800, poll_s=15, verbose=False, **kw):
    """Submit, wait, fetch. Returns {"answers": {...}, "batch": <final status>}."""
    job = submit(prompts, model=model, **kw)
    final = wait(job["id"], timeout_s=timeout_s, poll_s=poll_s, verbose=verbose)
    answers = results(job["id"]) if final.get("status") == "completed" else {}
    return {"answers": answers, "batch": final, "id": job["id"]}
