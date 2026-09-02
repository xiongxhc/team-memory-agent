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


class PushNotVerified(RuntimeError):
    """The remote does not hold what a caller was about to claim it holds."""


def _config(path: Path, key: str) -> str | None:
    done = subprocess.run(["git", "-C", str(path), "config", "--get", key],
                          capture_output=True, text=True)
    return done.stdout.strip() or None


def _upstream(path: Path) -> tuple[str, str, str]:
    """(remote, remote ref, branch) the checked-out branch publishes to."""
    branch = _git(path, "symbolic-ref", "--short", "HEAD").stdout.strip()
    remote = _config(path, f"branch.{branch}.remote")
    if remote is None:
        remotes = _git(path, "remote").stdout.split()
        if len(remotes) != 1:
            raise PushNotVerified(
                f"branch {branch} names no remote and the repo has "
                f"{len(remotes)} of them")
        remote = remotes[0]
    ref = _config(path, f"branch.{branch}.merge") or f"refs/heads/{branch}"
    return remote, ref, branch


def head_commit(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def commit_contains(path: Path, commit: str, rel_path: str) -> bool:
    """Is `rel_path` in the tree of `commit`? Asks the object store, not the disk:
    a file that is on disk but ignored, or staged but never committed, is absent."""
    done = subprocess.run(["git", "-C", str(path), "cat-file", "-e",
                           f"{commit}:{rel_path}"], capture_output=True, text=True)
    return done.returncode == 0


def remote_reaches(path: Path, commit: str) -> bool:
    """Does the branch's configured remote ref resolve to, or descend from, `commit`?

    Asked of the remote itself with `ls-remote`, never of a local tracking ref —
    a tracking ref happily claims a push landed when it did not.
    """
    remote, ref, _ = _upstream(path)
    listed = _git(path, "ls-remote", remote, ref).stdout.split()
    if not listed:
        return False
    if listed[0] == commit:
        return True
    _git(path, "fetch", "-q", remote, ref)
    done = subprocess.run(["git", "-C", str(path), "merge-base", "--is-ancestor",
                           commit, listed[0]], capture_output=True, text=True)
    return done.returncode == 0


def push_verified(path: Path) -> str:
    """`git push`, then prove the remote ref holds the pushed commit. Returns it.

    `push` above is the daily run's hand: it succeeds whenever git exits 0, and a
    push that lands on another ref exits 0 too. This one is for callers that go on
    to tell a human the commit is published. Raises `PushNotVerified`, or
    `CalledProcessError` when git itself fails.
    """
    commit = head_commit(path)
    _git(path, "push")
    if not remote_reaches(path, commit):
        remote, ref, _ = _upstream(path)
        raise PushNotVerified(f"{remote} {ref} does not hold {commit} after push")
    return commit
