"""Declutter: turn raw session turns into durable harness knowledge, a digest and a cursor.

The raw session log is append-only and stays untouched. This module reads only the turns the cursor
has not covered, asks one cheap model to distil them, writes the result into the harness (policies as
prompt notes, facts as memories, procedures as skills), then writes a short digest and advances the
cursor. Running it twice over the same turns is a no-op.

Trigger this from a workflow signal when enough data has accumulated - never from a blind cron.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import core

SESSION_DIR = Path(os.environ.get("PRIME_SESSION_DIR", "~/.prime/agent/sessions")).expanduser()
CURSOR_PATH = Path(os.environ.get("PRIME_LEARN_CURSOR", str(SESSION_DIR / ".cursor.json"))).expanduser()
DISTILL_MODEL = os.environ.get("PRIME_LEARN_MODEL", "openrouter/z-ai/glm-5.3-flash")
MAX_TURNS_PER_RUN = 120          # never feed an unbounded transcript
MAX_POLICIES, MAX_MEMORIES, MAX_SKILLS = 2, 3, 1

PROMPT = """You distil a working session into durable knowledge for an agent harness.

Rules:
- A POLICY is a behavioural rule the agent must follow every turn (commit granularity, incremental
  delivery, verification before claiming done). Put it in "policies".
- A MEMORY is a durable fact or preference (tools, paths, conventions). Put it in "memories".
- A SKILL is a repeatable procedure. Most sessions produce none.
- Never store secrets, tokens or credentials.
- Evidence must quote the user verbatim, or name a commit. No evidence -> do not write.
- Max {mp} policies, {mm} memories, {ms} skills. Prefer updating an existing entry over duplicating.

Existing entries (update instead of duplicating):
{titles}

Session turns since the last run:
{delta}

Return ONE json object and nothing else:
{{"policies":[{{"title","content","evidence"}}],"memories":[{{"title","content","evidence"}}],
 "skills":[{{"title","content"}}],"examples":[{{"situation","user_phrasing","agent_should"}}],
 "digest":"markdown: decisions, corrections, open threads (<=1200 chars)"}}"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_cursor():
    try:
        return json.loads(CURSOR_PATH.read_text())
    except Exception:
        return {}


def save_cursor(cursor):
    CURSOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_PATH.write_text(json.dumps(cursor, indent=1))


def _text_of(message):
    parts = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p).strip()


def read_delta(session_id, *, session_dir=None, cursor=None, limit=MAX_TURNS_PER_RUN):
    """Turns the cursor has not covered yet, as a compact transcript."""
    cursor = cursor if cursor is not None else load_cursor()
    offset = int((cursor.get(session_id) or {}).get("offset", 0))
    path = Path(session_dir or SESSION_DIR) / f"{session_id}.jsonl"
    if not path.exists():
        return {"text": "", "offset": offset, "turns": 0, "corrections": 0, "path": str(path)}
    lines = path.read_text(errors="replace").splitlines()
    fresh = lines[offset:]
    out, turns, corrections = [], 0, 0
    for line in fresh[-limit:]:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        msg = rec.get("message") or {}
        role = msg.get("role")
        text = _text_of(msg)
        if role not in ("user", "assistant") or not text:
            continue
        turns += 1
        if role == "user" and re.search(r"\b(no|not|again|instead|stop|don't|should|must|always|"
                                        r"never|I said|I want|why did)\b", text, re.I):
            corrections += 1
        out.append(f"[{role}] {text[:1500]}")
    return {"text": "\n\n".join(out), "offset": len(lines), "turns": turns,
            "corrections": corrections, "path": str(path)}


def existing_titles(global_=False, limit=80):
    try:
        import rlm
        state = rlm.get_harness_state(global_=global_)
    except Exception:
        return []
    titles = []
    for kind in ("prompt", "memory", "skill"):
        for entry in (state.entries.get(kind) or {}).values():
            titles.append(f"{kind}: {entry.title}")
    return titles[:limit]


async def distill(text, titles, *, model=DISTILL_MODEL):
    prompt = PROMPT.format(mp=MAX_POLICIES, mm=MAX_MEMORIES, ms=MAX_SKILLS,
                           titles="\n".join(titles) or "(none)", delta=text)
    _handle, parsed, used = await core.run_child("declutter", prompt, "policies",
                                                 models=[model], timeout=300)
    return parsed or {}, used


def apply(parsed, *, global_=False):
    """Write the distilled entries, updating a near-duplicate instead of creating one."""
    import rlm
    written = {"policies": 0, "memories": 0, "skills": 0, "updated": 0}

    def upsert(kind, item, cap):
        title = str(item.get("title") or "").strip()[:120]
        content = str(item.get("content") or "").strip()
        if not title or not content:
            return
        hits = []
        try:
            hits = rlm.harness.search(title, kind={"policies": "prompt", "memories": "memory",
                                                   "skills": "skill"}[kind], limit=3, global_=global_)
        except Exception:
            pass
        near = next((h for h in hits if h.title.lower()[:40] == title.lower()[:40]), None)
        if near is not None:
            rlm.harness.update_memory(near.id, title, content, global_=global_) if kind == "memories" else None
            written["updated"] += 1
            return
        if written[kind] >= cap:
            return
        if kind == "policies":
            rlm.harness.create_prompt_note(title, content, global_=global_)
        elif kind == "memories":
            rlm.harness.create_memory(title, content, global_=global_)
        else:
            rlm.harness.create_skill(title, content, global_=global_)
        written[kind] += 1

    for item in (parsed.get("policies") or [])[:MAX_POLICIES]:
        upsert("policies", item, MAX_POLICIES)
    for item in (parsed.get("memories") or [])[:MAX_MEMORIES]:
        upsert("memories", item, MAX_MEMORIES)
    for item in (parsed.get("skills") or [])[:MAX_SKILLS]:
        upsert("skills", item, MAX_SKILLS)
    return written


def compact(session_id, digest, *, session_dir=None):
    path = Path(session_dir or SESSION_DIR) / f"{session_id}.digest.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = _now()
    body = f"# Session digest\n\n_updated {stamp}_\n\n{digest.strip()}\n"
    path.write_text(body)
    return str(path)


async def run(session_id, *, session_dir=None, mode="balanced", global_=False, dry_run=False,
        cursor=None, model=DISTILL_MODEL):
    """read_delta -> distill -> apply -> compact -> advance the cursor. Idempotent per cursor."""
    cursor = cursor if cursor is not None else load_cursor()
    delta = read_delta(session_id, session_dir=session_dir, cursor=cursor)
    if not delta["text"].strip():
        return {"action": "idle", "reason": "no new turns", "turns": 0}
    titles = existing_titles(global_=global_)
    parsed, used = await distill(delta["text"], titles, model=model)
    written = apply(parsed, global_=global_)
    digest_path = compact(session_id, parsed.get("digest") or "", session_dir=session_dir)
    if dry_run:
        return {"action": "dry-run", "turns": delta["turns"], "corrections": delta["corrections"],
                "written": written, "digest": digest_path, "model": used, "parsed": parsed}
    cursor[session_id] = {"offset": delta["offset"], "turns": delta["turns"],
                          "corrections": delta["corrections"], "updated": _now(), "mode": mode}
    save_cursor(cursor)
    return {"action": "learned", "turns": delta["turns"], "corrections": delta["corrections"],
            "written": written, "digest": digest_path, "model": used}
