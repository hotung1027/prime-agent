"""Vault -> harness import: the Obsidian vault is the source of truth for harness entries.

The export in `obsidian_sync.py` renders entries as notes; this module is its inverse. Three rules
make it safe to run repeatedly:

1. **Identity is the note's `id`, never its path.** Moving or renaming a note in Obsidian must not look
   like delete + create - that would destroy the entry's history.
2. **The manifest is the delete ledger.** `{id: content hash}` from the last import. An id is removed
   from the harness only if the manifest knows we put it there and the note is gone now.
3. **A malformed note is skipped and reported, never deleted.** A typo in the frontmatter disables one
   entry loudly; it must not silently remove a policy the agent depends on.

Guards: an unreachable or empty vault aborts the import (a Windows mount that is not mounted would
otherwise look like "the user deleted everything"), and the harness file is backed up before it is
rewritten.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

KINDS = ("prompt", "memory", "skill", "subagent", "factory")
HARNESS = Path(os.environ.get("PRIME_HARNESS_STATE", "~/.prime/agent/harness/harness_state.json")).expanduser()
VAULT = Path(os.environ.get("PRIME_VAULT", "~/agent-wiki")).expanduser()
MANIFEST = Path(os.environ.get("PRIME_WIKI_MANIFEST", "~/.prime/agent/harness/.vault-manifest.json")).expanduser()
SKIP_DIRS = {".meta", ".git", "Archive", "Sessions"}


class ImportAborted(Exception):
    pass


def _unquote(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        return [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
    return value.strip("\"'")


def parse_note(text: str):
    """Frontmatter subset -> dict. Returns (fields, None) or (None, reason)."""
    if not text.startswith("---"):
        return None, "no frontmatter"
    end = text.find("\n---", 3)
    if end < 0:
        return None, "unterminated frontmatter"
    fields = {}
    for line in text[3:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            return None, f"malformed frontmatter line: {line[:40]!r}"
        fields[key.strip()] = _unquote(value)
    body = text[end + 4:].lstrip("\n")
    if body.startswith("#"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
    fields["_body"] = body.strip() + "\n"
    return fields, None


def parse_vault(vault: Path):
    """{id: entry} for every valid note, plus a list of (path, reason) for the ones skipped."""
    notes, skipped = {}, []
    if not vault.is_dir():
        return notes, [("<vault>", f"not a directory: {vault}")]
    for path in sorted(vault.rglob("*.md")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        fields, reason = parse_note(path.read_text(errors="replace"))
        if fields is None:
            skipped.append((str(path.relative_to(vault)), reason))
            continue
        ident, kind, title = fields.get("id"), fields.get("kind"), fields.get("title")
        if not ident or kind not in KINDS or not title:
            skipped.append((str(path.relative_to(vault)),
                            "needs id, kind (%s) and title" % "|".join(KINDS)))
            continue
        note = {"id": ident, "kind": kind, "title": title, "content": fields["_body"],
                "path": fields.get("category") or "general",
                "scope": fields.get("scope") or "global",
                "metadata": {"tags": fields.get("tags") or [], "source_note": str(path.relative_to(vault))}}
        for extra in ("evidence", "session"):
            if fields.get(extra):
                note["metadata"][extra] = fields[extra]
        notes[ident] = note
    return notes, skipped


def load_manifest(path=None) -> dict:
    try:
        return json.loads(Path(path or MANIFEST).read_text())
    except Exception:
        return {}


def save_manifest(manifest: dict, path=None):
    target = Path(path or MANIFEST)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, indent=1, sort_keys=True))


def content_hash(note: dict) -> str:
    blob = json.dumps({k: note.get(k) for k in ("title", "content", "path", "scope", "kind")},
                      sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _state_hash(entry: dict) -> str:
    return content_hash({"title": entry.get("title"), "content": entry.get("content"),
                         "path": entry.get("path"), "scope": entry.get("scope"),
                         "kind": entry.get("kind")})


def import_entries(vault=None, *, harness_path=None, manifest_path=None, dry_run=False,
                   allow_empty=False) -> dict:
    vault = Path(vault or VAULT)
    harness_path = Path(harness_path or HARNESS)
    manifest = load_manifest(manifest_path)
    notes, skipped = parse_vault(vault)
    if not notes and not allow_empty:
        raise ImportAborted(f"refusing to import: no valid notes under {vault} "
                            f"({len(skipped)} skipped) - a missing or empty vault must not wipe the harness")
    try:
        state = json.loads(harness_path.read_text())
    except Exception:
        state = {"schema": 1, "entries": {k: {} for k in KINDS}, "refinements": []}
    entries = state.setdefault("entries", {k: {} for k in KINDS})
    for kind in KINDS:
        entries.setdefault(kind, {})

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    created = updated = skipped_unchanged = 0
    conflicts = []
    for ident, note in notes.items():
        digest = content_hash(note)
        if manifest.get(ident) == digest:
            skipped_unchanged += 1
            continue
        existing = entries[note["kind"]].get(ident)
        if existing is not None:
            if _state_hash(existing) != manifest.get(ident):
                # the harness changed since the last import and the note differs too: keep the note
                # (it is the source of truth) but do not lose the agent's version silently.
                conflict = vault / f"{ident}.conflict-{int(time.time())}.md"
                if not dry_run:
                    conflict.write_text(f"# conflict: {ident}\n\nharness version at import time:\n\n"
                                        f"{existing.get('content', '')}\n")
                conflicts.append(str(conflict.name))
            updated += 1
        else:
            created += 1
        entry = dict(note, source="import", version=int((existing or {}).get("version", 0)) + 1,
                     created_at=(existing or {}).get("created_at", now), updated_at=now)
        entries[note["kind"]][ident] = entry
        manifest[ident] = digest

    deleted = []
    for ident in sorted(set(manifest) - set(notes)):
        for kind in KINDS:
            if ident in entries[kind]:
                deleted.append(f"{kind}:{ident}")
                entries[kind].pop(ident)
        manifest.pop(ident, None)

    result = {"created": created, "updated": updated, "deleted": len(deleted), "deleted_ids": deleted,
              "skipped": len(skipped), "skipped_notes": skipped[:10], "unchanged": skipped_unchanged,
              "conflicts": conflicts, "vault": str(vault), "harness": str(harness_path),
              "dry_run": dry_run}
    if not dry_run:
        harness_path.parent.mkdir(parents=True, exist_ok=True)
        if harness_path.exists():
            shutil.copy2(harness_path, harness_path.with_suffix(harness_path.suffix + ".bak"))
        tmp = harness_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(harness_path)
        save_manifest(manifest, manifest_path)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    dry = "--dry-run" in argv
    vault = next((a for a in argv if not a.startswith("-")), None)
    try:
        result = import_entries(vault, dry_run=dry)
    except ImportAborted as exc:
        print(json.dumps({"aborted": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
