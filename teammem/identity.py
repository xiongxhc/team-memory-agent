"""Deterministic identity resolution — no LLM. Unknown identities are surfaced as
_unmapped/<raw>, never dropped: silent drops are how gaps hide."""

from pathlib import Path

import yaml


RESOURCE_FIELDS = {
    "github_repos": "github-repo",
    "gitlab_repos": "gitlab-repo",
    "slack_channels": "slack-channel",
    "feishu_channels": "feishu-channel",
    "discord_channels": "discord-channel",
}
IDENTITY_FIELDS = ("email", "github", "gitlab", "slack", "feishu", "discord")


def _read(config_dir: Path, name: str) -> dict:
    real, example = config_dir / f"{name}.yaml", config_dir / f"{name}.example.yaml"
    path = real if real.exists() else example
    return yaml.safe_load(path.read_text()) or {}


class IdentityMaps:
    def __init__(self, roster: dict, projects: dict):
        self._person_by_key: dict[tuple[str, str], str] = {}
        for slug, m in (roster.get("members") or {}).items():
            for kind in IDENTITY_FIELDS:
                field = "emails" if kind == "email" else kind
                for value in m.get(field) or []:
                    self._check_and_insert_person((kind, value.lower()), slug, value)
        project_defs = projects.get("projects") or {}
        area_defs = projects.get("areas") or {}
        hidden_slugs = projects.get("hidden_projects", [])
        if (
            not isinstance(hidden_slugs, list)
            or any(
                not isinstance(slug, str) or not slug.strip()
                for slug in hidden_slugs
            )
        ):
            raise ValueError("hidden_projects must be a list of non-empty strings")
        category_by_slug: dict[str, str] = {}

        def register_category(slug: str, category: str) -> None:
            previous = category_by_slug.get(slug)
            if previous is not None:
                raise ValueError(
                    f"slug collision: {slug!r} claimed by both "
                    f"{previous!r} and {category!r}"
                )
            category_by_slug[slug] = category

        for slug in project_defs:
            register_category(slug, "projects")
        for slug in area_defs:
            register_category(slug, "areas")
        for slug in hidden_slugs:
            register_category(slug, "hidden_projects")

        self._projection_by_slug: dict[str, str] = {}
        for slug, p in project_defs.items():
            mode = p["projection"] if "projection" in p else "full"
            if not isinstance(mode, str) or mode not in {"full", "count-only"}:
                raise ValueError(
                    f"invalid projection {mode!r} for project {slug!r}; "
                    "expected 'full' or 'count-only'"
                )
            self._projection_by_slug[slug] = mode
        self._projection_by_slug.update({slug: "area" for slug in area_defs})
        self._projection_by_slug.update({slug: "hidden" for slug in hidden_slugs})

        self._project_by_resource: dict[tuple[str, str], str] = {}
        self._resource_values: dict[tuple[str, str], str] = {}
        for slug, p in (*project_defs.items(), *area_defs.items()):
            for field, kind in RESOURCE_FIELDS.items():
                for value in p.get(field) or []:
                    self._check_and_insert_resource((kind, value.lower()), slug, value)
        self._names = {slug: (m.get("name") or slug) for slug, m in (roster.get("members") or {}).items()}

    def _check_and_insert_person(self, key: tuple[str, str], slug: str, raw_value: str) -> None:
        if key in self._person_by_key and self._person_by_key[key] != slug:
            raise ValueError(f"identity collision: {raw_value!r} claimed by both {self._person_by_key[key]!r} and {slug!r}")
        self._person_by_key[key] = slug

    def _check_and_insert_resource(self, key: tuple[str, str], slug: str, raw_value: str) -> None:
        if key in self._project_by_resource and self._project_by_resource[key] != slug:
            raise ValueError(f"resource collision: {raw_value!r} claimed by both {self._project_by_resource[key]!r} and {slug!r}")
        self._project_by_resource[key] = slug
        self._resource_values.setdefault(key, raw_value)

    @classmethod
    def load(cls, config_dir: Path) -> "IdentityMaps":
        return cls(_read(config_dir, "roster"), _read(config_dir, "projects"))

    def person(self, kind: str, value: str) -> str:
        if not value:
            return "_unmapped/(none)"
        return self._person_by_key.get((kind, value.lower()), f"_unmapped/{value}")

    def project(self, kind: str, value: str) -> str | None:
        return self._project_by_resource.get((kind, value.lower()))

    def projection(self, slug: str) -> str:
        return self._projection_by_slug.get(slug, "unclassified")

    def project_slugs(self) -> list[str]:
        return sorted(
            slug
            for slug, projection in self._projection_by_slug.items()
            if projection in {"full", "count-only"}
        )

    def area_slugs(self) -> list[str]:
        return sorted(
            slug
            for slug, projection in self._projection_by_slug.items()
            if projection == "area"
        )

    def resources(self, kind: str) -> dict[str, str]:
        return {self._resource_values[(resource_kind, value)]: slug
                for (resource_kind, value), slug in self._project_by_resource.items()
                if resource_kind == kind}

    def project_for_repo(self, path_with_namespace: str) -> str | None:
        return self.project("gitlab-repo", path_with_namespace)

    def project_for_channel(self, chat_id: str) -> str | None:
        return self.project("feishu-channel", chat_id)

    def mapped_channels(self) -> dict[str, str]:
        return self.resources("feishu-channel")

    def display_name(self, slug: str) -> str:
        return self._names.get(slug, slug)

    def slugs(self) -> list[str]:
        return sorted(self._names)
