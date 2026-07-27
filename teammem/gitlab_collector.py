"""Central baseline collector: commits + MRs per author from the self-hosted GitLab.
Fetching goes through an injected fetch_json(path, params) -> list callable —
tests pass fakes; production passes http_fetch_json(cfg)."""

import json
from collections.abc import Callable
from datetime import datetime, timedelta

from .config import Config
from .events import Event, event_hash
from .identity import IdentityMaps

_PER_PAGE = 100

FetchJson = Callable[[str, dict], list]


def http_fetch_json(cfg: Config) -> FetchJson:
    import requests
    session = requests.Session()
    session.headers["PRIVATE-TOKEN"] = cfg.gitlab_token

    def fetch(path: str, params: dict) -> list:
        r = session.get(f"{cfg.gitlab_url}/api/v4{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    return fetch


def _paginate(fetch_json: FetchJson, path: str, params: dict) -> list:
    out, page = [], 1
    while True:
        batch = fetch_json(path, {**params, "per_page": _PER_PAGE, "page": page})
        out.extend(batch)
        if len(batch) < _PER_PAGE:
            return out
        page += 1


def collect_gitlab(cfg: Config, ids: IdentityMaps, fetch_json: FetchJson,
                   now: datetime) -> list[Event]:
    since = (now - timedelta(days=cfg.since_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    events: list[Event] = []
    projects = _paginate(fetch_json, f"/groups/{cfg.gitlab_group}/projects",
                         {"include_subgroups": "true"})
    for p in projects:
        project = ids.project_for_repo(p["path_with_namespace"])
        # Default-branch commits only (no all=true): branch work appears at merge via MRs.
        # Revisit during live dry-run / M2 gap logic if branch-level visibility is needed.
        for c in _paginate(fetch_json, f"/projects/{p['id']}/repository/commits",
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
        for mr in _paginate(fetch_json, f"/projects/{p['id']}/merge_requests",
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
    return events
