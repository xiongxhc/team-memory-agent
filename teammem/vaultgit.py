"""Thin git wrapper for the vault repo. Deterministic hands: add -A, commit,
optional push. Accepted history is never rewritten; a rejected owned unpushed
commit may be rolled back with compare-and-swap. Push is operator opt-in."""

import os
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


class VerifiedTreeMismatch(RuntimeError):
    """Git's candidate tree differs from the verified filesystem projection."""


def _publication_guard(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--absolute-git-dir").stdout.strip()) / "teammem-publication-pending"


def _assert_publication_ready(path: Path) -> None:
    if _publication_guard(path).exists():
        raise VerifiedTreeMismatch(
            "an interrupted or rejected vault commit needs explicit recovery; "
            "inspect the private Git teammem-publication-pending guard before publishing")


def _head_or_none(path: Path) -> str | None:
    result = subprocess.run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def verify_tree_manifest(path: Path, tree: str, manifest: dict[str, bytes]) -> None:
    entries = _git(path, "ls-tree", "-rz", tree).stdout.split("\0")
    actual = {}
    for entry in filter(None, entries):
        metadata, name = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        if kind != "blob" or mode not in ("100644", "100755"):
            raise VerifiedTreeMismatch(f"unverified Git entry: {name}")
        actual[name] = oid
    if actual.keys() != manifest.keys():
        names = sorted(actual.keys() ^ manifest.keys())
        raise VerifiedTreeMismatch("Git tree differs from verified paths: " + ", ".join(names))
    names = list(manifest)
    if not names:
        return
    # One `cat-file --batch` process reads every blob, so the byte-for-byte
    # comparison costs one fork rather than one per vault file.
    output = subprocess.run(
        ["git", "-C", str(path), "cat-file", "--batch"],
        input="".join(f"{actual[name]}\n" for name in names).encode("ascii"),
        capture_output=True, check=True).stdout
    offset = 0
    for name in names:
        end = output.index(b"\n", offset)
        oid, kind, size = output[offset:end].split(b" ")
        start = end + 1
        blob = output[start:start + int(size)]
        offset = start + int(size) + 1
        if kind != b"blob" or oid.decode("ascii") != actual[name] or blob != manifest[name]:
            raise VerifiedTreeMismatch(f"Git tree differs from verified bytes: {name}")


def _current_ref(path: Path) -> str:
    result = subprocess.run(["git", "-C", str(path), "symbolic-ref", "-q", "HEAD"],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.stdout.strip() if result.returncode == 0 else "HEAD"


def _owns_commit(path: Path, ref: str, parent: str | None, commit: str) -> bool:
    ancestry = _git(path, "rev-list", "--parents", "-n", "1", commit).stdout.split()
    return (_current_ref(path) == ref and _head_or_none(path) == commit
            and ancestry == [commit] + ([parent] if parent else []))


def _reject_owned_commit(path: Path, ref: str, parent: str | None, rejected: str) -> None:
    """Remove only our rejected unpushed commit from the current branch.

    Hooks remain enabled. Working files and index remain available for diagnosis.
    A ref/parent change or failed compare-and-swap refuses rollback rather than
    rewriting somebody else's work.
    """
    if not _owns_commit(path, ref, parent, rejected):
        raise VerifiedTreeMismatch("rejected Git commit ownership changed; rollback refused")
    try:
        if parent is None:
            _git(path, "update-ref", "-d", ref, rejected)
        else:
            _git(path, "update-ref", ref, parent, rejected)
    except subprocess.CalledProcessError as error:
        raise VerifiedTreeMismatch("rejected Git commit ref changed; rollback refused") from error


def commit_all(path: Path, msg: str, *, verified_manifest: dict[str, bytes] | None = None) -> bool:
    _assert_publication_ready(path)
    _git(path, "add", "-A")
    if verified_manifest is not None:
        verify_tree_manifest(path, _git(path, "write-tree").stdout.strip(), verified_manifest)
    if not _git(path, "status", "--porcelain").stdout.strip():
        if verified_manifest is not None:
            verify_tree_manifest(path, "HEAD", verified_manifest)
        return False
    if verified_manifest is not None:
        ref = _current_ref(path)
        parent = _head_or_none(path)
        guard = _publication_guard(path)
        # Write before commit: a crash or failed rollback must not let the next
        # unattended run push an unverified ancestor. Only proven success or
        # proven rollback clears this guard; recovery is explicitly operator-owned.
        with guard.open("x", encoding="utf-8") as handle:
            handle.write(f"ref={ref}\nparent={parent or 'unborn'}\n")
            handle.flush()
            os.fsync(handle.fileno())
    try:
        _git(path, "commit", "-q", "-m", msg)
    except subprocess.CalledProcessError:
        if (verified_manifest is not None and _current_ref(path) == ref
                and _head_or_none(path) == parent):
            guard.unlink()
        raise
    if verified_manifest is not None:
        committed = head_commit(path)
        # Exact tip bytes do not validate hook-created ancestors or ref moves.
        # Leave unowned history and the recovery guard intact on uncertainty.
        if not _owns_commit(path, ref, parent, committed):
            raise VerifiedTreeMismatch("Git commit ownership changed; publication refused")
        try:
            verify_tree_manifest(path, committed, verified_manifest)
        except VerifiedTreeMismatch:
            _reject_owned_commit(path, ref, parent, committed)
            guard.unlink()
            raise
        if not _owns_commit(path, ref, parent, committed):
            raise VerifiedTreeMismatch("Git commit ownership changed; publication refused")
        guard.unlink()
    return True


def push(path: Path) -> None:
    _assert_publication_ready(path)
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


def push_verified(path: Path, commit: str) -> str:
    """`git push`, then prove the remote ref holds `commit`. Returns it.

    `push` above is the daily run's hand: it succeeds whenever git exits 0, and a
    push that lands on another ref exits 0 too. This one is for callers that go on
    to tell a human the commit is published. Raises `PushNotVerified`, or
    `CalledProcessError` when git itself fails.

    The caller names the commit rather than the helper reading HEAD again: another
    hand committing to the same vault between the two reads would otherwise slip
    an unchecked commit into whatever the caller goes on to claim.
    """
    _assert_publication_ready(path)
    _git(path, "push")
    if not remote_reaches(path, commit):
        remote, ref, _ = _upstream(path)
        raise PushNotVerified(f"{remote} {ref} does not hold {commit} after push")
    return commit
