# llm_wiki - knowledge-vault mirror

`obsidian_sync.py` mirrors the knowledge base into an Obsidian vault: one folder per topic plus a
mirror of the agent's own machines/decisions, written idempotently (unchanged files are skipped).

    import obsidian_sync
    report = obsidian_sync.sync()      # {"written": n, "unchanged": m, ...}
