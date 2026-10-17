#!/usr/bin/env python3
"""Install the repository's git hooks: git config core.hooksPath .githooks

Run once per clone:  python scripts/install_hooks.py
"""
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = ROOT / ".githooks"


def main() -> int:
    if not HOOKS.is_dir():
        print(f"no hook directory at {HOOKS}", file=sys.stderr)
        return 1
    for hook in HOOKS.iterdir():
        if hook.is_file() and not hook.name.startswith("."):
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    subprocess.run(["git", "-C", str(ROOT), "config", "core.hooksPath", ".githooks"], check=True)
    names = sorted(p.name for p in HOOKS.iterdir() if p.is_file() and not p.name.startswith("."))
    print(f"core.hooksPath = .githooks | active: {', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
