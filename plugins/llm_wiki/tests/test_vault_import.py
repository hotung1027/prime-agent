
import json, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import vault_import as vi

ok = []
def check(n, c, d=""):
    ok.append((n, bool(c), d)); print(f"{'PASS' if c else 'FAIL'}  {n}" + (f"   [{d}]" if not c else ""))

tmp = Path(tempfile.mkdtemp())
vault = tmp / "vault"; vault.mkdir()
hpath = tmp / "harness_state.json"; mpath = tmp / "manifest.json"

def note(ident, kind, title, body, category="general", **extra):
    fm = [f"id: {ident}", f"kind: {kind}", f"title: {title}", f"category: {category}", "tags: [demo]"]
    fm += [f"{k}: {v}" for k, v in extra.items()]
    return "---\n" + "\n".join(fm) + "\n---\n\n# " + title + "\n\n" + body + "\n"

# harness before import: mem-b (known, edited in the vault), mem-c (unknown to the manifest)
hpath.write_text(json.dumps({"schema": 1, "entries": {
    "prompt": {}, "memory": {"mem-b": {"id": "mem-b", "kind": "memory", "title": "old b",
                                      "content": "old body\n", "path": "general", "scope": "global",
                                      "version": 1},
                             "mem-c": {"id": "mem-c", "kind": "memory", "title": "keep me",
                                       "content": "not in the manifest\n", "path": "general",
                                       "scope": "global", "version": 1},
                             "mem-d": {"id": "mem-d", "kind": "memory", "title": "note was deleted",
                                       "content": "manifest knows me\n", "path": "general",
                                       "scope": "global", "version": 1}},
    "skill": {}, "subagent": {}, "factory": {}}, "refinements": []}))
mpath.write_text(json.dumps({"mem-b": "stale-hash-of-the-old-body",
                              "mem-d": "hash-of-the-deleted-note"}))

(vault / "a.md").write_text(note("mem-a", "memory", "new fact", "brand new"))
(vault / "b.md").write_text(note("mem-b", "memory", "renamed b", "edited body"))
(vault / "bad.md").write_text("no frontmatter at all\n")               # malformed

r = vi.import_entries(vault, harness_path=hpath, manifest_path=mpath)
check("created/updated/deleted/skipped are exact",
      (r["created"], r["updated"], r["deleted"], r["skipped"]) == (1, 1, 1, 1),
      f"{r['created']}/{r['updated']}/{r['deleted']}/{r['skipped']}")
state = json.loads(hpath.read_text())["entries"]["memory"]
check("the new note became an entry", "mem-a" in state and state["mem-a"]["content"].strip() == "brand new")
check("the edited note was updated", state["mem-b"]["title"] == "renamed b" and state["mem-b"]["version"] == 2)
check("the vanished manifest-known id was removed", "mem-d" not in state, str(list(state)))
check("an id the manifest never knew survives", "mem-c" in state, str(list(state)))
check("the malformed note was reported, not applied", any("bad.md" in s[0] for s in r["skipped_notes"]))
check("the harness was backed up before writing", hpath.with_suffix(".json.bak").exists())
check("a conflict file was written for the edited entry", any("conflict" in c for c in r["conflicts"]),
      str(r["conflicts"]))

r2 = vi.import_entries(vault, harness_path=hpath, manifest_path=mpath)
check("a second import is a no-op", (r2["created"], r2["updated"], r2["deleted"]) == (0, 0, 0), str(r2))

empty = tmp / "empty"; empty.mkdir()
try:
    vi.import_entries(empty, harness_path=hpath, manifest_path=mpath)
    check("an empty vault aborts the import", False, "no error")
except vi.ImportAborted as e:
    check("an empty vault aborts the import", "refusing to import" in str(e))

missing = tmp / "does-not-exist"
try:
    vi.import_entries(missing, harness_path=hpath, manifest_path=mpath)
    check("a missing vault aborts the import", False, "no error")
except vi.ImportAborted:
    check("a missing vault aborts the import", True)

passed = sum(1 for _, c, _ in ok if c)
print(f"\n{passed}/{len(ok)} checks passed")
sys.exit(0 if passed == len(ok) else 1)
