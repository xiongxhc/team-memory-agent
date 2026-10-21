import subprocess
from pathlib import Path

from teammem.vaultgit import ensure_repo, commit_all


def test_ensure_and_commit_cycle(tmp_path):
    ensure_repo(tmp_path)
    assert (tmp_path / ".git").is_dir()
    ensure_repo(tmp_path)                          # idempotent
    (tmp_path / "a.md").write_text("x")
    assert commit_all(tmp_path, "first") is True
    assert commit_all(tmp_path, "noop") is False   # clean tree -> no commit
    log = subprocess.run(["git", "-C", str(tmp_path), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "first" in log and "noop" not in log


def test_a_missing_git_object_is_a_tree_mismatch_not_a_crash(tmp_path):
    """A corrupt or partial object store answers `<oid> missing` to the batch
    reader; that is an unverifiable tree, refused with a clear code."""
    import pytest
    from teammem import vaultgit

    ensure_repo(tmp_path)
    (tmp_path / "note.md").write_text("hello\n")
    commit_all(tmp_path, "one")
    tree = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD^{tree}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    blob = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD:note.md"],
                          capture_output=True, text=True, check=True).stdout.strip()
    obj = tmp_path / ".git" / "objects" / blob[:2] / blob[2:]
    obj.chmod(0o600)
    obj.unlink()

    with pytest.raises(vaultgit.VerifiedTreeMismatch, match="git_object_missing"):
        vaultgit.verify_tree_manifest(tmp_path, tree, {"note.md": b"hello\n"})
