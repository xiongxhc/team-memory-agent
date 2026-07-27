"""Thin git wrapper for the vault repo. Deterministic hands: add -A, commit,
optional push. Never rewrites history; push only on explicit operator opt-in."""

import subprocess
from pathlib import Path


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True, check=True)


def ensure_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").is_dir():
        _git(path, "init", "-q")
        _git(path, "config", "user.name", "teammem")
        _git(path, "config", "user.email", "teammem@local")


def commit_all(path: Path, msg: str) -> bool:
    _git(path, "add", "-A")
    if not _git(path, "status", "--porcelain").stdout.strip():
        return False
    _git(path, "commit", "-q", "-m", msg)
    return True


def push(path: Path) -> None:
    _git(path, "push")
