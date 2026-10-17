#!/usr/bin/env python3
"""Prove the hooks in a throwaway repository: refuse bulk, refuse bad messages, accept a small commit."""
import subprocess, sys, tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOKS = REPO / ".githooks"
results = []

def check(name, ok, detail=""):
    results.append(ok); print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if not ok else ""))

def git(repo, *args, check=False):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)

def make_repo(tmp):
    git(tmp, "init", "-q"); git(tmp, "config", "user.email", "t@example.com")
    git(tmp, "config", "user.name", "t"); git(tmp, "config", "commit.gpgsign", "false")
    (tmp / ".githooks").mkdir()
    for hook in HOOKS.iterdir():
        if hook.is_file():
            dest = tmp / ".githooks" / hook.name
            dest.write_bytes(hook.read_bytes()); dest.chmod(0o755)
    git(tmp, "config", "core.hooksPath", ".githooks")

with tempfile.TemporaryDirectory() as d:
    tmp = Path(d); make_repo(tmp)

    # 1. a 1200-line staged commit is refused
    (tmp / "big.txt").write_text("\n".join(f"line {i}" for i in range(1200)) + "\n")
    git(tmp, "add", "big.txt")
    r = git(tmp, "commit", "-m", "feat(big): add a lot")
    check("1200-line commit refused", r.returncode != 0 and "REFUSED" in r.stderr, r.stderr[:200])
    check("refusal names the policy", "one commit = one responsibility" in r.stderr)

    # 2. --no-verify bypasses locally (CI still runs the check)
    r = git(tmp, "commit", "-m", "feat(big): add a lot", "--no-verify")
    check("--no-verify bypasses locally", r.returncode == 0, r.stderr[:200])

    # 3. a small commit passes, and a bad message is refused
    (tmp / "small.txt").write_text("hello\n")
    git(tmp, "add", "small.txt")
    r = git(tmp, "commit", "-m", "added a thing")
    check("non-conventional message refused", r.returncode != 0 and "conventional" in r.stderr, r.stderr[:200])
    r = git(tmp, "commit", "-m", "feat(small): add a file")
    check("200-line conventional commit accepted", r.returncode == 0, r.stderr[:300])

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
