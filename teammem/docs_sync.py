"""Copy per-project docs (architecture.md, summary.md) from the local Obsidian
vault into the team vault's Docs/ tree. Docs/ is unmanaged — render never
deletes it. [[wikilinks]] are flattened to plain text (GitLab web UI renders
them as empty repo-wiki pages). Idempotent: unchanged files are not rewritten.
Folder matching: an explicit `obsidian:` key in projects.yaml wins, else a
case/punctuation-insensitive name match against the Obsidian folder name."""

import re
from pathlib import Path

# Source vaults may use capitalized (Architecture.md) or lowercase names; try
# capitalized first, fall back to lowercase. Destinations are always lowercase —
# vault project notes link ../Docs/<slug>/architecture.md and Git web UIs are
# case-sensitive.
DOC_FILES = ("Architecture.md", "Summary.md")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def flatten_wikilinks(text: str) -> str:
    return re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
                  lambda m: m.group(2) or m.group(1), text)


def match_folders(projects: dict, obsidian_dir: Path) -> dict[str, Path]:
    by_norm = {_norm(p.name): p for p in obsidian_dir.iterdir() if p.is_dir()}
    out = {}
    for slug, p in (projects.get("projects") or {}).items():
        override = (p or {}).get("obsidian")
        folder = obsidian_dir / override if override else by_norm.get(_norm(slug))
        if folder is not None and folder.is_dir():
            out[slug] = folder
    return out


def sync_docs(projects: dict, obsidian_dir: Path, vault_dir: Path) -> dict:
    matched = match_folders(projects, obsidian_dir)
    copied = 0
    for slug, folder in sorted(matched.items()):
        for name in DOC_FILES:
            src = folder / name
            if not src.is_file():
                src = folder / name.lower()
                if not src.is_file():
                    continue
            text = flatten_wikilinks(src.read_text())
            dst = vault_dir / "Docs" / slug / name.lower()
            if dst.is_file() and dst.read_text() == text:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text)
            copied += 1
    return {"projects": len(matched), "copied": copied}
