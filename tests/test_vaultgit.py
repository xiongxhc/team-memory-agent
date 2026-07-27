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
