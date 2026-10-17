"""Content-addressed gate: is there new, worthwhile material to learn from?

ccache model: the work is keyed by its INPUTS, so a tick that finds unchanged inputs is a no-op. No
emitter, no cooperation from the agent, no missed turns.

Load budget (measured in tests):
  unchanged tick   one stat() + one small JSON read                  ~ tens of microseconds
  changed tick     hash ONLY the bytes appended since the cursor, in 1 MiB chunks
                   -> memory is bounded by the chunk, not by the session size
  AST hash         only for code files, on demand, and it ignores formatting/comments

The fingerprint is written after a decision (distil or "not enough yet"), so a repeated tick is a true
no-op and a crash simply re-runs the same delta.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from pathlib import Path

SESSION_DIR = Path(os.environ.get("PRIME_SESSION_DIR", "~/.prime/agent/sessions")).expanduser()
STATE_PATH = Path(os.environ.get("PRIME_LEARN_CURSOR", str(SESSION_DIR / ".fingerprint.json"))).expanduser()
HARNESS = Path(os.environ.get("PRIME_HARNESS_STATE", "~/.prime/agent/harness/harness_state.json")).expanduser()

CHUNK = 1 << 20                      # 1 MiB: bounds memory on any file size
CORRECTION = re.compile(r"\b(no|not|again|instead|stop|don't|should|must|always|never|I said|I want)\b", re.I)


def load_state(path=None) -> dict:
    try:
        return json.loads(Path(path or STATE_PATH).read_text())
    except Exception:
        return {}


def save_state(state: dict, path=None):
    target = Path(path or STATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(state, indent=1))


def _delta_counts(path: Path, offset: int):
    """Stream the bytes after `offset`: content hash + turn/token/correction counts, bounded memory."""
    digest = hashlib.sha256()
    turns = tokens = corrections = 0
    partial = b""
    with open(path, "rb") as handle:
        handle.seek(offset)
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            partial += chunk
            *lines, partial = partial.split(b"\n")
            for raw in lines:
                if b'"role"' not in raw:          # cheap reject before any JSON parsing
                    continue
                try:
                    record = json.loads(raw)
                except Exception:
                    continue
                message = record.get("message") or {}
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = " ".join(b.get("text", "") for b in (message.get("content") or [])
                                if isinstance(b, dict) and b.get("type") == "text")
                if not text.strip():
                    continue
                turns += 1
                tokens += len(text) // 4                     # cheap estimate; no tokenizer call
                if role == "user" and CORRECTION.search(text):
                    corrections += 1
    return digest.hexdigest(), turns, tokens, corrections


def _file_hash(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def ast_hash(source: str) -> str:
    """Structural hash: formatting and comment edits do NOT change it, real edits do."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "syntax:" + hashlib.sha256(source.encode()).hexdigest()[:16]
    return hashlib.sha256(ast.dump(tree, annotate_fields=False, include_attributes=False)
                          .encode()).hexdigest()


def fingerprint(session_id: str, *, session_dir=None, state=None, min_turns=8, min_corrections=1,
                min_tokens=0, persist=True):
    """Return what changed since the last decision, and whether it is worth a distil pass."""
    state = state if state is not None else load_state()
    previous = (state.get(session_id) or {})
    path = Path(session_dir or SESSION_DIR) / f"{session_id}.jsonl"
    if not path.exists():
        return {"changed": False, "enough": False, "reason": "no session log"}

    stat = path.stat()
    if previous.get("size") == stat.st_size and previous.get("mtime_ns") == stat.st_mtime_ns:
        # the cheap path: nothing was appended, so nothing to hash and nothing to learn
        return {"changed": False, "enough": False, "reason": "unchanged", "turns": 0, "tokens": 0,
                "corrections": 0, "offset": previous.get("offset", 0)}

    offset = int(previous.get("offset", 0)) if stat.st_size >= int(previous.get("size", 0)) else 0
    conv_hash, turns, tokens, corrections = _delta_counts(path, offset)
    entry = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "offset": stat.st_size,
             "conv_hash": conv_hash, "harness_hash": _file_hash(HARNESS),
             "turns": int(previous.get("turns", 0)) + turns,
             "tokens": int(previous.get("tokens", 0)) + tokens,
             "corrections": int(previous.get("corrections", 0)) + corrections,
             "last_checked": time.time()}
    enough = (entry["turns"] >= min_turns or entry["corrections"] >= min_corrections
              or entry["tokens"] >= min_tokens)
    result = {"changed": True, "enough": enough, "turns": turns, "tokens": tokens,
              "corrections": corrections, "offset": entry["offset"], "conv_hash": conv_hash,
              "totals": {k: entry[k] for k in ("turns", "tokens", "corrections")}}
    if persist:
        state[session_id] = entry
        save_state(state)
    return result


def reset_counters(session_id: str, *, state=None):
    """After a distil pass: keep the offset (nothing to re-read) and zero the accumulation."""
    state = state if state is not None else load_state()
    entry = state.get(session_id) or {}
    entry.update({"turns": 0, "tokens": 0, "corrections": 0})
    state[session_id] = entry
    save_state(state)
    return entry
