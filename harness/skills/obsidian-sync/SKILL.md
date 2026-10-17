---
name: obsidian-sync
description: Obsidian export: harness knowledge and project plans as linked markdown
---

Export the continual harness to the Obsidian vault as linked markdown.

    import obsidian_sync
    obsidian_sync.sync_all()                 # everything (recommended)
    obsidian_sync.sync()                     # harness mirror only
    obsidian_sync.export_runs()              # project plans from ~/.prime-pipeline runs
Vault: $HOME/agent-wiki (Windows C:\Users\hotun\agent-wiki).
Layout: Index.md (home MOC: topics, projects, harness) | <Topic>/Index.md topic roots (Rust, Julia,
<topic folders>) | Projects/<project>/Index.md and
Plans/<YYYY>/<MM>/<DD>/<run-tag>/{Index.md, task.md, research.md, spec.md, review.md, verify.md, ...}
| "Prime Agent/" harness mirror (Rules, Memories, Skills, Subagents, Machines, Categories,
Refinements.md). Tags: prime-agent/<kind>, scope/<global|local>, category/<path>, project/<p>,
practice/<t>, plan, plan-day. Aliases carry titles so [[id|Title]] renders readably; entry ids
mentioned in other notes are auto-linked. Idempotent (unchanged files are not rewritten) and
ONE-WAY: the harness JSON is the source of truth, vault edits are overwritten.
CLI: python3 ~/.prime/agent/pipelines/obsidian_sync.py [--dry-run]. Runs automatically from the
pipeline `run_end` hook and the 10-minute background loop.
