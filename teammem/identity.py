"""Deterministic identity resolution — no LLM. Unknown identities are surfaced as
_unmapped/<raw>, never dropped: silent drops are how gaps hide."""

from pathlib import Path

import yaml


def _read(config_dir: Path, name: str) -> dict:
    real, example = config_dir / f"{name}.yaml", config_dir / f"{name}.example.yaml"
    path = real if real.exists() else example
    return yaml.safe_load(path.read_text()) or {}


class IdentityMaps:
    def __init__(self, roster: dict, projects: dict):
        self._person_by_key: dict[tuple[str, str], str] = {}
        for slug, m in (roster.get("members") or {}).items():
            for email in m.get("emails") or []:
                self._check_and_insert_person(("email", email.lower()), slug, email)
            for username in m.get("gitlab") or []:
                self._check_and_insert_person(("gitlab", username.lower()), slug, username)
            for oid in m.get("feishu") or []:
                self._check_and_insert_person(("feishu", oid.lower()), slug, oid)
        self._project_by_repo: dict[str, str] = {}
        for slug, p in (projects.get("projects") or {}).items():
            for repo in p.get("gitlab_repos") or []:
                self._check_and_insert_repo(repo.lower(), slug, repo)
            for chan in p.get("feishu_channels") or []:
                self._check_and_insert_repo(chan.lower(), slug, chan)
        self._names = {slug: (m.get("name") or slug) for slug, m in (roster.get("members") or {}).items()}

    def _check_and_insert_person(self, key: tuple[str, str], slug: str, raw_value: str) -> None:
        if key in self._person_by_key and self._person_by_key[key] != slug:
            raise ValueError(f"identity collision: {raw_value!r} claimed by both {self._person_by_key[key]!r} and {slug!r}")
        self._person_by_key[key] = slug

    def _check_and_insert_repo(self, repo: str, slug: str, raw_repo: str) -> None:
        if repo in self._project_by_repo and self._project_by_repo[repo] != slug:
            raise ValueError(f"repo/channel collision: {raw_repo!r} claimed by both {self._project_by_repo[repo]!r} and {slug!r}")
        self._project_by_repo[repo] = slug

    @classmethod
    def load(cls, config_dir: Path) -> "IdentityMaps":
        return cls(_read(config_dir, "roster"), _read(config_dir, "projects"))

    def person(self, kind: str, value: str) -> str:
        if not value:
            return "_unmapped/(none)"
        return self._person_by_key.get((kind, value.lower()), f"_unmapped/{value}")

    def project_for_repo(self, path_with_namespace: str) -> str | None:
        return self._project_by_repo.get(path_with_namespace.lower())

    def project_for_channel(self, chat_id: str) -> str | None:
        return self._project_by_repo.get(chat_id.lower())

    def mapped_channels(self) -> dict[str, str]:
        return {k: v for k, v in self._project_by_repo.items() if k.startswith("oc_")}

    def display_name(self, slug: str) -> str:
        return self._names.get(slug, slug)

    def slugs(self) -> list[str]:
        return sorted(self._names)
