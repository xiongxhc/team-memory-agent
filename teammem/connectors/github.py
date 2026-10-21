"""GitHub repository connector restricted to configured project mappings."""

import json
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from teammem.config import Config
from teammem.events import Event, event_hash
from teammem.identity import IdentityMaps
from teammem.metrics import CommitCountScope, WeeklyCommitCount

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
        now = now.astimezone(timezone.utc)
        fetch = self._fetch or self.http_fetch(cfg)
        since = now - timedelta(days=cfg.since_days)
        since_text = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        repositories = [
            (repository, project, ids.projection(project))
            for repository, project in ids.resources("github-repo").items()
        ]
        count_projects = {
            project for _, project, projection in repositories
            if projection == "count-only"
        }
        count_week_starts: tuple[str, ...] = ()
        count_since_text = ""
        if count_projects:
            count_weeks = self._count_weeks(settings.options)
            current_date = now.date()
            current_monday = current_date - timedelta(days=current_date.weekday())
            oldest_monday = current_monday - timedelta(weeks=count_weeks - 1)
            count_week_starts = tuple(
                (oldest_monday + timedelta(weeks=offset)).isoformat()
                for offset in range(count_weeks)
            )
            count_since_text = f"{oldest_monday.isoformat()}T00:00:00Z"

        events: list[Event] = []
        warnings: list[str] = []
        commit_counts: Counter[tuple[str, str, str]] = Counter()
        count_scope_keys = {
            (project, week_start)
            for project in count_projects
            for week_start in count_week_starts
        }
        for repository, project, projection in repositories:
            base = f"/repos/{repository}"
            if projection == "count-only":
                for commit in self._paginate(
                    fetch,
                    f"{base}/commits",
                    {"since": count_since_text},
                ):
                    commit_author = (commit.get("commit") or {}).get("author") or {}
                    commit_date = self._timestamp(commit_author["date"]).astimezone(
                        timezone.utc
                    ).date()
                    week_start = commit_date - timedelta(days=commit_date.weekday())
                    week_start_text = week_start.isoformat()
                    if (project, week_start_text) not in count_scope_keys:
                        warnings.append(
                            f"github count-only response for {project} has UTC week "
                            f"{week_start_text} outside requested replacement scopes; "
                            "ignored"
                        )
                        continue
                    author = commit.get("author") or {}
                    person = ids.person("github", author.get("login", ""))
                    if person.startswith("_unmapped/") and commit_author.get("email"):
                        person = ids.person("email", commit_author["email"])
                    commit_counts[(project, week_start_text, person)] += 1
                continue

            for commit in self._paginate(fetch, f"{base}/commits", {"since": since_text}):
                events.append(self._commit_event(commit, ids, project))
            for pull_request in self._pull_requests(
                fetch,
                f"{base}/pulls",
                {"state": "all", "sort": "updated", "direction": "desc"},
                since,
            ):
                events.append(
                    self._pull_request_event(
                        pull_request,
                        ids,
                        project,
                        repository,
                    )
                )
        return CollectionResult(
            events=tuple(events),
            warnings=tuple(warnings),
            commit_counts=tuple(
                WeeklyCommitCount(project, week_start, person, commit_count)
                for (project, week_start, person), commit_count
                in sorted(commit_counts.items())
            ),
            commit_count_scopes=tuple(
                CommitCountScope(project, week_start)
                for project in sorted(count_projects)
                for week_start in count_week_starts
            ),
        )

    @staticmethod
    def _count_weeks(options: dict) -> int:
        raw_count_weeks = options.get("count_weeks", 4)
        if type(raw_count_weeks) is not int or not 1 <= raw_count_weeks <= 52:
            raise ValueError("count_weeks must be an integer from 1 to 52")
        return raw_count_weeks

    @staticmethod
    def _paginate(fetch: FetchJson, path: str, params: dict) -> list:
        events, page = [], 1
        while True:
            batch = fetch(path, {**params, "per_page": _PER_PAGE, "page": page})
            events.extend(batch)
            if len(batch) < _PER_PAGE:
                return events
            page += 1

    @classmethod
    def _pull_requests(
        cls,
        fetch: FetchJson,
        path: str,
        params: dict,
        since: datetime,
    ) -> list:
        pull_requests, page = [], 1
        while True:
            batch = fetch(path, {**params, "per_page": _PER_PAGE, "page": page})
            for pull_request in batch:
                if cls._timestamp(pull_request["updated_at"]) < since:
                    return pull_requests
                pull_requests.append(pull_request)
            if len(batch) < _PER_PAGE:
                return pull_requests
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
