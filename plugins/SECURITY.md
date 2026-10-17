# Security

## No credentials in this repository

Every credential is supplied through the environment (see README). The one file-based source,
`langfuse.json`, is git-ignored and must never be committed; `langfuse.example.json` is the committed
template.

Before every commit:

    git grep -nE '(sk|pk)-[A-Za-z0-9_-]{20,}' && echo 'LEAK - do not commit'

Use a strict pattern: `sk-` alone also matches innocuous text such as `task-queue`.

## If a key is committed

1. Rotate the key at the provider immediately - deleting the file does not remove it from history.
2. Rewrite history only after rotation, and force-push the fork (never upstream).
3. Record the rotation in `CHANGELOG.md`.

## Report

Open a private security advisory or contact the repository owner; do not file a public issue with the
key in it.
