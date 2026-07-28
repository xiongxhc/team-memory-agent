"""GitHub repository connector restricted to configured project mappings."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from teammem.config import Config
from teammem.events import Event, event_hash
from teammem.identity import IdentityMaps

from .base import CollectionResult
from .config import ConnectorSettings


GITHUB_API_URL = "https://api.github.com"
GITHUB_API_VERSION = "2026-03-10"
_PER_PAGE = 100
FetchJson = Callable[[str, dict], list]


class GitHubConnector:
    name = "github"

    def __init__(self, fetch: FetchJson | None = None):
        self._fetch = fetch

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]:
        return [] if cfg.github_token else ["TEAMMEM_GITHUB_TOKEN"]

    def http_fetch(self, cfg: Config) -> FetchJson:
        import requests

        session = requests.Session()
        session.headers.update({
            "Authorization": f"Bearer {cfg.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        })

        def fetch(path: str, params: dict) -> list:
            response = session.get(f"{GITHUB_API_URL}{path}", params=params, timeout=30)
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
        fetch = self._fetch or self.http_fetch(cfg)
        since = now - timedelta(days=cfg.since_days)
        since_text = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        events: list[Event] = []
        for repository, project in ids.resources("github-repo").items():
            base = f"/repos/{repository}"
            for commit in self._paginate(fetch, f"{base}/commits", {"since": since_text}):
                events.append(self._commit_event(commit, ids, project))
            for pull_request in self._paginate(fetch, f"{base}/pulls", {"state": "all"}):
                if self._timestamp(pull_request["updated_at"]) >= since:
                    events.append(self._pull_request_event(pull_request, ids, project, repository))
        return CollectionResult(events=tuple(events))

    @staticmethod
    def _paginate(fetch: FetchJson, path: str, params: dict) -> list:
        events, page = [], 1
        while True:
            batch = fetch(path, {**params, "per_page": _PER_PAGE, "page": page})
            events.extend(batch)
            if len(batch) < _PER_PAGE:
                return events
            page += 1

    @staticmethod
    def _timestamp(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _commit_event(commit: dict, ids: IdentityMaps, project: str | None) -> Event:
        author = commit.get("author") or {}
        commit_author = (commit.get("commit") or {}).get("author") or {}
        person = ids.person("github", author.get("login", ""))
        if person.startswith("_unmapped/") and commit_author.get("email"):
            person = ids.person("email", commit_author["email"])
        return Event(
            person=person,
            project=project,
            ts=commit_author["date"],
            source="github",
            kind="commit",
            summary=commit["commit"]["message"].splitlines()[0],
            refs=json.dumps({"sha": commit["sha"], "url": commit.get("html_url")}),
            raw=json.dumps(commit),
            hash=commit["sha"],
        )

    @staticmethod
    def _pull_request_event(
        pull_request: dict,
        ids: IdentityMaps,
        project: str | None,
        repository: str,
    ) -> Event:
        user = pull_request.get("user") or {}
        return Event(
            person=ids.person("github", user.get("login", "")),
            project=project,
            ts=pull_request["updated_at"],
            source="github",
            kind="pr",
            summary=f"[{pull_request['state']}] {pull_request['title']}",
            refs=json.dumps({"number": pull_request["number"], "url": pull_request.get("html_url")}),
            raw=json.dumps(pull_request),
            hash=event_hash(
                "pr",
                repository,
                str(pull_request["number"]),
                pull_request["state"],
                pull_request["updated_at"],
            ),
        )
