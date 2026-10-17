
"""Export the prime-agent continual harness to an Obsidian vault.

One-way, idempotent: the harness state (~/.prime/agent/harness/harness_state.json) is the source
of truth; this writes markdown notes with YAML frontmatter, tags, and [[wikilinks]] so the
knowledge is browsable and searchable in Obsidian.

Layout written inside the vault:
    Prime Agent/Index.md                     MOC: counts + links to everything
    Prime Agent/Rules/<id>.md                prompt notes (user rules/policies)
    Prime Agent/Memories/<id>.md
    Prime Agent/Skills/<id>.md
    Prime Agent/Subagents/<id>.md
    Prime Agent/Machines/<id>.md
    Prime Agent/Categories/<path>.md         one MOC per category, links its entries
    Prime Agent/Refinements.md               refinement event log

Tags: #prime-agent/<kind>, #scope/<global|local>, #category/<path>, plus any metadata tags.
Usage: python obsidian_sync.py [--vault DIR] [--dry-run] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import os
from pathlib import Path

AGENT_DIR = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR") or Path.home() / ".prime" / "agent")
GLOBAL_STATE = AGENT_DIR / "harness" / "harness_state.json"
DEFAULT_VAULT = Path(os.environ.get("PRIME_VAULT", "~/agent-wiki")).expanduser()
ROOT = "Prime Agent"
KIND_DIR = {"prompt": "Rules", "memory": "Memories", "skill": "Skills",
            "subagent": "Subagents", "factory": "Machines"}
KIND_TAG = {"prompt": "rule", "memory": "memory", "skill": "skill",
            "subagent": "subagent", "factory": "machine"}


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "entry"


def _yaml(value):
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml(v) for v in value) + "]"
    text = str(value).replace('"', '\\"')
    return f'"{text}"'


def load_state(path=GLOBAL_STATE):
    if not Path(path).exists():
        return {"entries": {}, "refinements": []}
    return json.loads(Path(path).read_text())


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def render_entry(entry, all_ids):
    kind = entry["kind"]
    tags = [f"prime-agent/{KIND_TAG.get(kind, kind)}", f"scope/{entry.get('scope', 'local')}",
            f"category/{_slug(entry.get('path') or 'general')}"]
    for extra in (entry.get("metadata") or {}).get("tags", []) or []:
        tags.append(str(extra))
    title = entry.get("title") or entry["id"]
    lines = ["---",
             f"title: {_yaml(title)}",
             f"id: {_yaml(entry['id'])}",
             f"kind: {_yaml(kind)}",
             f"scope: {_yaml(entry.get('scope', 'local'))}",
             f"category: {_yaml(entry.get('path') or 'general')}",
             f"source: {_yaml(entry.get('source', 'agent'))}",
             f"version: {entry.get('version', 1)}",
             f"created: {_yaml(entry.get('created_at', ''))}",
             f"updated: {_yaml(entry.get('updated_at', ''))}",
             f"tags: {_yaml(tags)}",
             f"aliases: {_yaml([title])}",
             "---", ""]
    lines += [f"# {title}", "",
              f"[[Prime Agent/Index|Prime Agent]] · [[Prime Agent/Categories/{_slug(entry.get('path') or 'general')}|{entry.get('path') or 'general'}]]", ""]
    body = entry.get("content") or ""
    # cross-link other harness entries mentioned by id or title
    for other_id in all_ids:
        if other_id != entry["id"] and other_id in body and f"[[{other_id}]]" not in body:
            body = body.replace(other_id, f"[[{other_id}]]")
    lines += [body, ""]
    if entry.get("reference"):
        lines += ["## Reference", "```json", json.dumps(entry["reference"], indent=2), "```", ""]
    if entry.get("arguments"):
        lines += ["## Arguments", "```json", json.dumps(entry["arguments"], indent=2), "```", ""]
    return "\n".join(lines) + "\n"


def export_machines(vault, dry_run=False):
    """Library machines (~/.prime/agent/machines/<name>/MACHINE.md) into Prime Agent/Machines/."""
    written = []
    lib = AGENT_DIR / "machines"
    if not lib.exists():
        return written
    for mach in sorted(p for p in lib.iterdir() if p.is_dir()):
        src = mach / "MACHINE.md"
        if not src.exists():
            continue
        text = src.read_text(errors="ignore")
        title = mach.name
        m = re.search(r"^description:\s*(.+)$", text, re.M)
        desc = (m.group(1).strip().strip('"') if m else "")[:140]
        note = ("---\n" + f"title: {_yaml(title)}\n" + f"kind: \"machine\"\n"
                + f"category: \"machines\"\n" + f"tags: {_yaml(['prime-agent/machine', 'scope/global'])}"
                + "\n" + f"aliases: {_yaml([title])}" + "\n---\n\n"
                + f"# {title}\n\n{desc}\n\n"
                + f"[[Prime Agent/Index|Prime Agent]] · [[Prime Agent/Categories/machines|machines]]\n\n"
                + "```markdown\n" + text + "\n```\n")
        rel = f"{ROOT}/Machines/{_slug(mach.name)}.md"
        if not dry_run and _write_if_changed(vault / rel, note):
            written.append(rel)
        elif not dry_run:
            pass
        else:
            written.append(rel)
    return written


def sync(vault=DEFAULT_VAULT, dry_run=False, quiet=False):
    vault = Path(vault)
    state = load_state()
    entries = [e for group in state.get("entries", {}).values() for e in group.values()]
    all_ids = sorted(e["id"] for e in entries)
    written, unchanged = [], 0

    def emit(rel, text, always=False):
        nonlocal unchanged
        path = vault / rel
        if dry_run:
            written.append(rel)
            return
        if _write_if_changed(path, text):
            written.append(rel)
        else:
            unchanged += 1

    by_category = {}
    for e in entries:
        rel = f"{ROOT}/{KIND_DIR.get(e['kind'], 'Other')}/{_slug(e['id'])}.md"
        emit(rel, render_entry(e, all_ids))
        by_category.setdefault(_slug(e.get("path") or "general"), []).append(e)

    # category MOCs
    for cat, items in sorted(by_category.items()):
        lines = ["---", f"title: {_yaml('Category: ' + cat)}", f"tags: {_yaml(['prime-agent/category', f'category/{cat}'])}",
                 f"aliases: {_yaml(['category-' + cat])}", "---", "", f"# Category: {cat}", "",
                 f"[[Prime Agent/Index|Prime Agent]]", ""]
        for e in sorted(items, key=lambda x: (x["kind"], x["id"])):
            lines.append(f"- [[{_slug(e['id'])}|{e.get('title') or e['id']}]]  `{e['kind']}`")
        lines.append("")
        emit(f"{ROOT}/Categories/{cat}.md", "\n".join(lines))

    # refinements log
    refs = state.get("refinements", [])
    lines = ["---", "title: \"Refinements\"", "tags: [prime-agent/refinements]", "---", "",
             "# Refinements", "", f"{len(refs)} recorded refinement event(s).", ""]
    for r in refs:
        lines += [f"## {r.get('created_at', '')} — {r.get('trigger', '')}",
                  f"- outcome: {r.get('outcome', '')}",
                  f"- evidence: {r.get('evidence', '')}"]
        for c in r.get("changes", []) or []:
            lines.append(f"- change: {c}")
        lines.append("")
    emit(f"{ROOT}/Refinements.md", "\n".join(lines))

    # index MOC
    counts = {}
    for e in entries:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    lines = ["---", "title: \"Prime Agent Harness\"", "tags: [prime-agent/index]", "---", "",
             "# Prime Agent Harness", "",
             "Exported from the continual harness. Do not edit here expecting it to persist — "
             "edit the harness (memories/prompt notes) and re-run `obsidian_sync.py`.", "",
             f"- entries: {len(entries)}  ({', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))})",
             f"- refinements: {len(refs)}", f"- source: `{GLOBAL_STATE}`", "",
             "## Categories", ""]
    for cat in sorted(by_category):
        lines.append(f"- [[{cat}|category: {cat}]]  ({len(by_category[cat])})")
    lines += ["", "## Entries", ""]
    for kind_dir in ("Rules", "Memories", "Skills", "Subagents", "Machines"):
        items = [e for e in entries if KIND_DIR.get(e["kind"]) == kind_dir]
        if not items:
            continue
        lines.append(f"### {kind_dir}")
        for e in sorted(items, key=lambda x: x["id"]):
            lines.append(f"- [[{_slug(e['id'])}|{e.get('title') or e['id']}]]  "
                         f"`{e.get('path') or 'general'}`  v{e.get('version', 1)}")
        lines.append("")
    lines += ["## Logs", "", "- [[Refinements]]", ""]
    emit(f"{ROOT}/Index.md", "\n".join(lines))

    machines_written = export_machines(vault, dry_run=dry_run)
    summary = {"vault": str(vault), "entries": len(entries), "categories": len(by_category),
               "written": len(written) + len(machines_written), "unchanged": unchanged,
               "machines": len(machines_written), "dry_run": dry_run}
    if not quiet:
        print(json.dumps(summary, indent=2))
    return summary




# --------------------------------------------------------------------------- #
# Project plans (from pipeline runs) and practice roots
# --------------------------------------------------------------------------- #

RUNS_ROOT = Path(os.path.expanduser("~/.prime-factory"))
PROJECTS_DIR = "Projects"
RUN_FILES = ["task.md", "research.md", "spec.md", "review.md", "verify.md",
             "visual.md", "perf.md", "infra.md", "cicd.md", "changed.txt", "merge.md"]

# topic -> keywords that route knowledge/plans into a practice root
# Topic routing is project knowledge, not plugin code: supply it as JSON through PRIME_WIKI_TOPICS
# ({"topic": ["keyword", ...]}). Without it every note lands in the default folder.
def _load_topics():
    path = os.environ.get("PRIME_WIKI_TOPICS")
    if path:
        try:
            with open(path) as fh:
                return {str(k): [str(w) for w in v] for k, v in json.load(fh).items()}
        except Exception:
            return {}
    return {}


TOPICS = _load_topics()


def _guess_project(run_dir: Path) -> str:
    """Project name from the run's task brief (a /home/<user>/<project> path), else 'unassigned'."""
    for name in ("task.md", "spec.md"):
        p = run_dir / name
        if not p.exists():
            continue
        text = p.read_text(errors="ignore")
        m = re.search(r"/home/[A-Za-z0-9_.-]+/([A-Za-z0-9_.-]+)", text)
        if m:
            slug = m.group(1).strip(".")
            if slug not in ("tmp", "home", "agent"):
                return slug
    return "unassigned"


def _run_title(run_dir: Path) -> str:
    p = run_dir / "task.md"
    if p.exists():
        first = p.read_text(errors="ignore").strip().splitlines()
        for line in first[:6]:
            line = line.strip("# ").strip()
            if line:
                return line[:90]
    return run_dir.name


def export_runs(vault, runs_root=RUNS_ROOT, dry_run=False, quiet=True):
    """One folder per factory run under Projects/<project>/Plans/<YYYY>/<MM>/<DD>/<tag>/."""
    vault = Path(vault)
    runs_root = Path(runs_root)
    written = []
    if not runs_root.exists():
        return written
    per_project = {}
    for run in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        if not (run / "task.md").exists():
            continue
        project = _slug(_guess_project(run)).lower()
        stamp = max((f.stat().st_mtime for f in run.iterdir() if f.is_file()), default=run.stat().st_mtime)
        import datetime as _dt
        when = _dt.datetime.fromtimestamp(stamp)
        rel_dir = f"{PROJECTS_DIR}/{project}/Plans/{when:%Y}/{when:%m}/{when:%d}/{run.name}"
        title = _run_title(run)
        lines = ["---", f"title: {_yaml(title)}", f"project: {_yaml(project)}",
                 f"run: {_yaml(run.name)}", f"date: {when:%Y-%m-%d}",
                 f"tags: {_yaml(['plan', f'project/{project}', f'date/{when:%Y-%m-%d}'])}", "---", "",
                 f"# {title}", "",
                 f"[[{PROJECTS_DIR}/{project}/Index|{project}]] · "
                 f"[[{PROJECTS_DIR}/{project}/Plans/{when:%Y}/{when:%m}/{when:%d}|{when:%Y-%m-%d}]]",
                 "", f"- run: `{run.name}`", f"- source: `{run}`", "",
                 "## Artifacts", ""]
        for name in RUN_FILES:
            src = run / name
            if not src.exists():
                continue
            text = src.read_text(errors="ignore")
            (vault / rel_dir).mkdir(parents=True, exist_ok=True) if not dry_run else None
            if not dry_run:
                _write_if_changed(vault / rel_dir / (Path(name).stem + ".md"), text)
            lines.append(f"- [[{Path(name).stem}]]")
            written.append(f"{rel_dir}/{name}")
        lines.append("")
        if not dry_run:
            _write_if_changed(vault / rel_dir / "Index.md", "\n".join(lines))
        written.append(f"{rel_dir}/Index.md")
        per_project.setdefault(project, []).append((when, run.name, title, rel_dir))

    for project, items in per_project.items():
        lines = ["---", f"title: {_yaml('Project: ' + project)}",
                 f"tags: {_yaml(['project', f'project/{project}'])}",
                 f"aliases: {_yaml([project])}", "---", "", f"# Project: {project}", "",
                 f"[[Software-Practices/Index|Software Practices]]", "", "## Plans", ""]
        for when, tag, title, rel_dir in sorted(items, reverse=True):
            day = "/".join(rel_dir.split("/")[3:6])
            lines.append(f"- {when:%Y-%m-%d} — [[{rel_dir}/Index|{title}]]  `{tag}`  "
                         f"( [[{PROJECTS_DIR}/{project}/Plans/{day}|day {when:%Y-%m-%d}]] )")
        lines.append("")
        if not dry_run:
            _write_if_changed(vault / PROJECTS_DIR / project / "Index.md", "\n".join(lines))
        written.append(f"{PROJECTS_DIR}/{project}/Index.md")

        by_day = {}
        for when, tag, title, rel_dir in items:
            by_day.setdefault(when.strftime("%Y/%m/%d"), []).append((tag, title, rel_dir))
        for day_key, runs in by_day.items():
            dlines = ["---", f"title: {_yaml(project + ' ' + day_key)}",
                      f"tags: {_yaml(['plan-day', f'project/{project}'])}", "---", "",
                      f"# {project} — {day_key}", "",
                      f"[[{PROJECTS_DIR}/{project}/Index|{project}]]", ""]
            for tag, title, rel_dir in sorted(runs):
                dlines.append(f"- [[{rel_dir}/Index|{title}]]  `{tag}`")
            dlines.append("")
            if not dry_run:
                _write_if_changed(vault / PROJECTS_DIR / project / "Plans" / (day_key + ".md"),
                                  "\n".join(dlines))
            written.append(f"{PROJECTS_DIR}/{project}/Plans/{day_key}.md")
    return written


def export_practices(vault, dry_run=False):
    """One root folder per recurring topic, each an index over harness entries + plans."""
    vault = Path(vault)
    state = load_state()
    entries = [(k, e) for k, group in state.get("entries", {}).items() for e in group.values()]
    plans = []
    for p in (vault / PROJECTS_DIR).rglob("Plans/*/*/*/*/Index.md") if (vault / PROJECTS_DIR).exists() else []:
        plans.append((p, p.read_text(errors="ignore")))
    written = []
    for topic, words in sorted(TOPICS.items()):
        hits = []
        for kind, e in entries:
            blob = (e.get("title", "") + " " + (e.get("content") or "") + " " +
                    str(e.get("path", ""))).lower()
            if any(re.search(r"(?<![a-z])" + re.escape(w.strip()) + r"(?![a-z])", blob) for w in words):
                hits.append(e)
        plan_hits = [p for p, txt in plans
                     if any(re.search(r"(?<![a-z])" + re.escape(w.strip()) + r"(?![a-z])", txt.lower())
                            for w in words)]
        if not hits and not plan_hits:
            continue
        lines = ["---", f"title: {_yaml('Practice: ' + topic)}",
                 f"tags: {_yaml(['practice', f'practice/{topic.lower()}'])}",
                 f"aliases: {_yaml([topic])}", "---", "", f"# {topic}", "",
                 f"[[Index|Vault Home]]", "", "## Knowledge (continual harness)", ""]
        for e in sorted(hits, key=lambda x: (x["kind"], x["id"])):
            lines.append(f"- [[{_slug(e['id'])}|{e.get('title') or e['id']}]]  `{e['kind']}`")
        lines += ["", "## Plans that touch this", ""]
        for p in sorted(plan_hits)[:40]:
            rel = p.relative_to(vault).with_suffix("")
            lines.append(f"- [[{rel}|{p.parent.parent.name}/{p.parent.name}]]")
        lines.append("")
        if not dry_run:
            _write_if_changed(vault / topic / "Index.md", "\n".join(lines))
        written.append(f"{topic}/Index.md")

    home = ["---", "title: \"Vault Home\"", "tags: [index]", "---", "", "# Vault Home", "",
            "## Topics", ""]
    for topic in sorted(TOPICS):
        if (vault / topic / "Index.md").exists():
            home.append(f"- [[{topic}/Index|{topic}]]")
    home += ["", "## Projects", ""]
    if (vault / PROJECTS_DIR).exists():
        for p in sorted(x for x in (vault / PROJECTS_DIR).iterdir() if x.is_dir()):
            home.append(f"- [[{PROJECTS_DIR}/{p.name}/Index|{p.name}]]")
    home += ["", "## Continual harness", "", "- [[Prime Agent/Index|Prime Agent]]", ""]
    if not dry_run:
        _write_if_changed(vault / "Index.md", "\n".join(home))
    written.append("Index.md")
    return written


def sync_all(vault=DEFAULT_VAULT, dry_run=False, quiet=False):
    """Harness mirror + project plans + practice roots."""
    summary = sync(vault, dry_run=dry_run, quiet=True)
    runs = export_runs(vault, dry_run=dry_run)
    practices = export_practices(vault, dry_run=dry_run)
    summary["plan_files"] = len(runs)
    summary["practice_notes"] = len(practices)
    if not quiet:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", default=str(DEFAULT_VAULT))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    sync_all(args.vault, dry_run=args.dry_run, quiet=args.quiet)
