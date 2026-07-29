"""Push a reviewed bundle into the team inbox repo. The bundle file is the
privacy boundary: only what the member reviewed leaves the machine."""

import subprocess
from pathlib import Path

from . import bundle
from .config import Config
from .state import DraftState


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"{' '.join(cmd)} failed:\n{(e.stderr or e.stdout).strip()}")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args])


def push(cfg: Config, date: str) -> Path:
    src = cfg.workdir / "out" / f"bundle-{cfg.member}-{date}.json"
    if not src.exists():
        raise SystemExit(f"no bundle at {src} — run `memberkit draft` first")
    try:
        data = bundle.prepare_bundle(src, cfg.member, date)
    except ValueError as exc:
        raise SystemExit(f"{src}: {exc}") from exc
    state = DraftState(cfg.workdir / "state.json")
    state.refresh(date, discovered=[], current=data)

    clone = cfg.workdir / "inbox"
    if not clone.exists():
        _run(["git", "clone", cfg.inbox_url, str(clone)])
    else:
        try:
            _git(clone, "pull", "--rebase")
        except SystemExit:
            subprocess.run(["git", "-C", str(clone), "rebase", "--abort"],
                           capture_output=True, text=True)
            raise

    dest = clone / cfg.member / src.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_bundle(dest, data)
    if _git(clone, "status", "--porcelain").stdout.strip():
        _git(clone, "add", f"{cfg.member}/{src.name}")
        _git(clone, "commit", "-m", f"bundle: {cfg.member} {date}")
    ahead = subprocess.run(
        ["git", "-C", str(clone), "rev-list", "@{u}..HEAD"],
        capture_output=True, text=True)
    if ahead.returncode == 0 and not ahead.stdout.strip():
        state.record_push(date, data["events"])
        return dest
    _git(clone, "push")
    state.record_push(date, data["events"])
    return dest
