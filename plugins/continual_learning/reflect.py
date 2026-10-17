
"""End-of-day reflection: digest the day's work into reusable knowledge, via the batch API.

The one workload that genuinely suits a fixed 24h window: it runs at the end of the day, nothing is
blocked on it, its consumer is TOMORROW's work, it fans out over many independent items (one prompt
per run / session / trace snapshot), and the 50-62% batch discount matters at that volume.

    import reflect
    job = reflect.submit()            # end of day: gather + one batch job (cheap, 24h window)
    note = reflect.collect()          # next morning: fetch, synthesise, write the vault note

Design: SUBMIT NOW, COLLECT LATER. The window cannot be shortened (the API accepts only "24h"), so the
job id is persisted under ~/.prime/agent/reflections/<day>.json and collected on a later run.

What it writes: <vault>/Software-Practices/Daily-Reflection/<YYYY-MM-DD>.md - a digest plus PROPOSED
harness entries (practices / rules / skills / memories). Proposals are text only; nothing is applied
to the harness automatically, because a bad rule silently steers every later run.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import os
from pathlib import Path

import batch as batch_api
import decision
import engine
import tracing

BATCH_MODEL = "deepseek/deepseek-v4.1-flash:batch"     # out $0.336/Mtok vs $0.89
SYNC_MODEL = "openrouter/deepseek/deepseek-v4.1-flash"  # for the final synthesis / dry runs
AGENT_DIR = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR") or Path.home() / ".prime" / "agent")
RUNS = Path.home() / ".prime-factory"
TRACES = AGENT_DIR / "traces" / "local"
STATE = AGENT_DIR / "reflections"
VAULT = Path(os.environ.get("PRIME_VAULT", "~/agent-wiki")).expanduser()

DIGEST_PROMPT = """You are digesting one piece of a day's engineering work into reusable knowledge.

Material source: {source}
Content:
---
{material}
---

Extract ONLY what would change how a future run behaves. Reply with one json object:
{{"title": "<short>", "happened": "<2 sentences>",
  "practices": ["<a procedure worth repeating>"],
  "pitfalls": ["<a failure mode and its fix>"],
  "candidate_rules": ["<a narrow behavioural rule, or omit>"],
  "candidate_skills": ["<a reusable procedure with its trigger, or omit>"],
  "candidate_memories": ["<a durable fact, or omit>"]}}
Keep every string under 160 characters. Omit a key entirely when nothing qualifies - do not invent."""

SYNTH_PROMPT = """You are writing the end-of-day reflection note from per-item digests.

Digests:
{digests}

Write markdown with: `## What happened`, `## Practices worth repeating`, `## Pitfalls and fixes`,
`## Proposed rules`, `## Proposed skills`, `## Proposed memories`. Deduplicate aggressively - merge
overlapping items and drop anything trivial or already known. Under each proposal add the exact
harness call that would create it, e.g.
`rlm.harness.create_prompt_note("<title>", "<content>", id="<slug>", path="policy", global_=True)`.
End with one line: how many proposals you would actually apply."""


def _read(path, limit=6000):
    try:
        text = Path(path).read_text(errors="ignore")
    except OSError:
        return ""
    return text[:limit]


def gather(day=None, *, max_runs=12):
    """Collect the day's raw material as one prompt per item (custom_id -> prompt)."""
    day = day or dt.date.today().isoformat()
    items = {}
    for run in sorted((p for p in RUNS.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:max_runs]:
        stamp = dt.datetime.fromtimestamp(run.stat().st_mtime).date().isoformat()
        if stamp != day:
            continue
        parts = []
        for name in ("task.md", "research.md", "spec.md", "review.md", "verify.md", "visual.md",
                     "perf.md", "changed.txt"):
            text = _read(run / name, 3500)
            if text:
                parts.append(f"### {name}\n{text}")
        if parts:
            items[f"run-{run.name}"] = DIGEST_PROMPT.format(
                source=f"factory run {run.name}", material="\n\n".join(parts)[:12000])
    trace_file = TRACES / f"events-{day}.jsonl"
    if trace_file.exists():
        rows = []
        for line in trace_file.open():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("kind") == "span-end" and rec["name"].startswith("role:"):
                a = rec.get("attributes") or {}
                rows.append(f"{rec['name']} model={a.get('model')} ms={a.get('duration_ms')} "
                            f"error={a.get('error')}")
        if rows:
            items["traces"] = DIGEST_PROMPT.format(
                source=f"trace timeline {day}",
                material="\n".join(rows[-120:])[:12000])
    harness = AGENT_DIR / "harness" / "harness_state.json"
    if harness.exists():
        try:
            entries = json.loads(harness.read_text())
            fresh = [e for group in entries.get("entries", {}).values() for e in group.values()
                     if str(e.get("updated_at", "")).startswith(day)]
            if fresh:
                items["harness"] = DIGEST_PROMPT.format(
                    source="harness entries changed today",
                    material="\n".join(f"- {e['kind']} {e['id']}: {e.get('title')}" for e in fresh)[:8000])
        except ValueError:
            pass
    return items


def submit(day=None, *, model=BATCH_MODEL, items=None):
    """Submit one batch job for the day. Returns {"day","id","items","status", "mode"}."""
    day = day or dt.date.today().isoformat()
    items = items if items is not None else gather(day)
    allowed, failed = __import__("routing").batch_gate(
        single_turn=True, blocking=False, latency_ok=True, items=len(items) + 1, savings_matter=True)
    if not allowed:
        return {"day": day, "id": None, "items": len(items), "mode": "sync",
                "blocked_by": failed, "note": "batch gate refused; run reflect_dry(day) instead"}
    job = batch_api.submit(items, model=model, max_tokens=900)
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{day}.json").write_text(json.dumps(
        {"day": day, "batch_id": job["id"], "model": model, "items": sorted(items),
         "submitted_at": dt.datetime.now().isoformat()}, indent=2))
    tracing.event("reflection-submitted", day=day, batch_id=job["id"], items=len(items), model=model)
    return {"day": day, "id": job["id"], "items": len(items), "status": job["status"], "mode": "batch"}


def _digests(day):
    state = STATE / f"{day}.json"
    if not state.exists():
        raise RuntimeError(f"no reflection submitted for {day}")
    rec = json.loads(state.read_text())
    try:
        results = batch_api.results(rec["batch_id"])
    except RuntimeError as exc:
        if "404" in str(exc):          # results are only published once the job is complete
            return rec, {}
        raise
    return rec, results


def collect(day=None, *, write=True, dry_run=False):
    """Fetch a submitted reflection and write the vault note (+ proposals)."""
    day = day or dt.date.today().isoformat()
    rec, results = _digests(day)
    usable = {k: v for k, v in results.items() if isinstance(v, str)}
    if not usable:
        return {"day": day, "collected": 0, "status": "not_ready", "batch_id": rec["batch_id"]}
    digests = "\n\n".join(f"### {k}\n{v}" for k, v in usable.items())
    reply = await_sync(decision, SYNTH_PROMPT.format(digests=digests[:14000]))
    note = (f"# Reflection — {day}\n\n"
            f"_Digested from {len(usable)} item(s) via `{rec['model']}` (batch), synthesised by "
            f"`{SYNC_MODEL}`._\n\n{reply}\n")
    out = VAULT / "Software-Practices" / "Daily-Reflection" / f"{day}.md"
    if write and not dry_run:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(note)
    tracing.event("reflection-collected", day=day, items=len(usable), note=str(out))
    return {"day": day, "collected": len(usable), "note": str(out), "text": reply}


def tick(now=None, *, submit_hour=23, collect_after_h=0.25, verbose=True):
    """One scheduler pass. Pure Python - no model turn, no tokens.

    - at/after submit_hour, if no job was submitted today: submit it (cheap, 24h window)
    - if a job exists and its results are ready: collect them into the vault note
    Returns a small dict describing what it did, so a caller (or a heartbeat) can report it.
    """
    now = now or dt.datetime.now()
    day = now.date().isoformat()
    state = STATE / f"{day}.json"
    if not state.exists():
        if now.hour >= submit_hour:
            job = submit(day)
            if verbose:
                print(f"[reflect] {now:%H:%M} submitted {job.get('id')} for {day} ({job.get('items')} items)")
            return {"action": "submitted", **job}
        return {"action": "waiting", "day": day, "reason": f"before submit_hour {submit_hour}:00"}
    rec = json.loads(state.read_text())
    submitted = dt.datetime.fromisoformat(rec["submitted_at"])
    if (now - submitted).total_seconds() < collect_after_h * 3600:
        return {"action": "queued", "day": day, "batch_id": rec["batch_id"]}
    try:
        result = collect(day)
    except Exception as exc:
        return {"action": "error", "day": day, "error": f"{type(exc).__name__}: {exc}"}
    if result.get("collected"):
        if verbose:
            print(f"[reflect] {now:%H:%M} collected {result['collected']} digest(s) -> {result['note']}")
        return {"action": "collected", **{k: v for k, v in result.items() if k != "text"}}
    return {"action": "not_ready", "day": day, "batch_id": rec["batch_id"]}


def await_sync(module, prompt):
    """One synchronous synthesis call (a chat model, not batch)."""
    import urllib.request
    key = module._key()
    api_model = SYNC_MODEL.split("/", 1)[1] if SYNC_MODEL.startswith("openrouter/") else SYNC_MODEL
    # A reasoning model spends the budget on reasoning first: at max_tokens=2000 the digest came back
    # with finish_reason "length", 7433 chars of reasoning and EMPTY content. Digests do not need
    # reasoning, so turn it off and leave headroom.
    body = {"model": api_model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4000, "reasoning": {"enabled": False}}
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            data = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"sync synthesis HTTP {exc.code}: {exc.read()[:300].decode(errors='ignore')}") from None
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
