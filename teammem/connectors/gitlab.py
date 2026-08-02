"""GitLab connector adapter with legacy event identities preserved."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from teammem.config import Config
from teammem.events import Event, event_hash
from teammem.identity import IdentityMaps

from .base import CollectionResult
from .config import ConnectorSettings


_PER_PAGE = 100
FetchJson = Callable[[str, dict], list]


def _parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GitLabConnector:
    name = "gitlab"

    def __init__(self, fetch_json: FetchJson | None = None):
        self._fetch_json = fetch_json

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]:
        fields = (
            ("TEAMMEM_GITLAB_URL", cfg.gitlab_url),
            ("TEAMMEM_GITLAB_TOKEN", cfg.gitlab_token),
            ("TEAMMEM_GITLAB_GROUP", cfg.gitlab_group),
        )
        return [name for name, value in fields if not value]

    def http_fetch_json(self, cfg: Config) -> FetchJson:
        import requests

        session = requests.Session()
        session.headers["PRIVATE-TOKEN"] = cfg.gitlab_token

        def fetch(path: str, params: dict) -> list:
            response = session.get(f"{cfg.gitlab_url}/api/v4{path}", params=params, timeout=30)
            response.raise_for_status()
            return response.json()

        return fetch

    def collect(
        self,
        cfg: Config,
        ids: IdentityMaps,
        settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult:
        fetch_json = self._fetch_json or self.http_fetch_json(cfg)
        events, warnings = self._collect_events(cfg, ids, fetch_json, now)
        return CollectionResult(events=tuple(events), warnings=tuple(warnings))

    @staticmethod
    def _paginate(fetch_json: FetchJson, path: str, params: dict) -> list:
        out, page = [], 1
        while True:
            batch = fetch_json(path, {**params, "per_page": _PER_PAGE, "page": page})
            out.extend(batch)
            if len(batch) < _PER_PAGE:
                return out
            page += 1

    def _collect_events(
        self, cfg: Config, ids: IdentityMaps, fetch_json: FetchJson, now: datetime
    ) -> tuple[list[Event], list[str]]:
        since_time = now - timedelta(days=cfg.since_days)
        since = since_time.strftime("%Y-%m-%dT%H:%M:%SZ")
        events: list[Event] = []
        warnings: list[str] = []
        projects = self._paginate(fetch_json, f"/groups/{cfg.gitlab_group}/projects",
                                  {
                                      "include_subgroups": "true",
                                      "with_shared": "false",
        })
        for p in projects:
            project = ids.project_for_repo(p["path_with_namespace"])
            created_at = p.get("created_at")
            if created_at and _parse_iso8601(created_at) >= since_time:
                creator = self._creator_username(fetch_json, p)
                if creator is None:
                    warnings.append(
                        "repository creator lookup failed for "
                        f"{p['path_with_namespace']}; creation deferred"
                    )
                else:
                    events.append(Event(
                        person=ids.person("gitlab", creator),
                        project=project,
                        ts=created_at,
                        source="gitlab",
                        kind="repo",
                        summary=f"[created] {p['path_with_namespace']}",
                        refs=json.dumps({"id": p["id"], "url": p.get("web_url")}),
                        raw=json.dumps(p),
                        hash=event_hash("repo", str(p["id"]), "created"),
                    ))
            # Default-branch commits only (no all=true): branch work appears at merge via MRs.
            # Revisit during live dry-run / M2 gap logic if branch-level visibility is needed.
            for c in self._paginate(fetch_json, f"/projects/{p['id']}/repository/commits",
                                    {"since": since}):
                events.append(Event(
                    person=ids.person("email", c.get("author_email", "")),
                    project=project,
                    ts=c["committed_date"],
                    source="gitlab",
                    kind="commit",
                    summary=c["title"],
                    refs=json.dumps({"sha": c["id"], "url": c.get("web_url")}),
                    raw=json.dumps(c),
                    hash=c["id"],
                ))
            for mr in self._paginate(fetch_json, f"/projects/{p['id']}/merge_requests",
                                     {"updated_after": since}):
                events.append(Event(
                    person=ids.person("gitlab", (mr.get("author") or {}).get("username", "")),
                    project=project,
                    ts=mr.get("merged_at") or mr["updated_at"],
                    source="gitlab",
                    kind="mr",
                    summary=f"[{mr['state']}] {mr['title']}",
                    refs=json.dumps({"iid": mr["iid"], "url": mr.get("web_url")}),
                    raw=json.dumps(mr),
                    hash=event_hash("mr", str(p["id"]), str(mr["iid"]), mr["state"]),
                ))
            for issue in self._paginate(fetch_json, f"/projects/{p['id']}/issues",
                                        {"updated_after": since}):
                created_at = issue.get("created_at")
                if created_at and _parse_iso8601(created_at) >= since_time:
                    author = issue.get("author") or {}
                    events.append(Event(
                        person=ids.person("gitlab", author.get("username", "")),
                        project=project,
                        ts=created_at,
                        source="gitlab",
                        kind="issue",
                        summary=f"[opened] {issue['title']}",
                        refs=json.dumps({"iid": issue["iid"], "url": issue.get("web_url")}),
                        raw=json.dumps(issue),
                        hash=event_hash("issue", str(p["id"]), str(issue["iid"]),
                                        "opened"),
                    ))
                closed_at = issue.get("closed_at")
                if closed_at and _parse_iso8601(closed_at) >= since_time:
                    closer = issue.get("closed_by") or {}
                    events.append(Event(
                        person=ids.person("gitlab", closer.get("username", "")),
                        project=project,
                        ts=closed_at,
                        source="gitlab",
                        kind="issue",
                        summary=f"[closed] {issue['title']}",
                        refs=json.dumps({"iid": issue["iid"], "url": issue.get("web_url")}),
                        raw=json.dumps(issue),
                        hash=event_hash("issue", str(p["id"]), str(issue["iid"]),
                                        "closed"),
                    ))
        return events, warnings

    @staticmethod
    def _creator_username(fetch_json: FetchJson, p: dict) -> str | None:
        if not p.get("creator_id"):
            return None
        try:
            user = fetch_json(f"/users/{p['creator_id']}", {"page": 1})
        except Exception:
            return None
        username = user.get("username") if isinstance(user, dict) else None
        return username if isinstance(username, str) and username else None
