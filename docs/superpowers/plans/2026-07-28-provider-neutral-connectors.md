# Provider-Neutral Connectors Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a built-in connector interface, GitHub/GitLab/Slack/Feishu/Discord adapters, and one portable `teammem run-daily` workflow without changing the private Feishu deployment.

**Architecture:** A static registry exposes five dependency-light connector modules. Each adapter validates its own configuration and returns existing `Event` values; a separate daily workflow orchestrates enabled adapters and existing local services. Provider credentials load from a user-only hub environment file and may be overridden by the process environment.

**Tech Stack:** Python 3.11+, dataclasses, Protocol, requests, PyYAML, SQLite, pytest

## Global Constraints

- Installation enables no connector and performs no network request.
- GitHub, GitLab, Slack, Feishu, and Discord are official built-in adapters.
- Chat adapters query only project-mapped channel IDs and never DMs.
- Slack is optional public functionality: bot-token polling, top-level messages only, no user token, no thread replies.
- The private internal deployment remains Feishu-based and is not modified by this repository.
- `teammem-bundle/v1` remains frozen.
- Existing GitLab/Feishu event sources, hashes, and configuration continue to work.
- Tests use fixture transports, temporary configuration, and temporary SQLite only.
- Provider setup details must match current official documentation.

---

### Task 1: Hub Environment and Connector Configuration

**Files:**
- Create: `config/connectors.example.yaml`
- Create: `teammem/connectors/__init__.py`
- Create: `teammem/connectors/base.py`
- Create: `teammem/connectors/config.py`
- Modify: `teammem/config.py`
- Test: `tests/test_connector_config.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces: `ConnectorSettings(name: str, enabled: bool, options: dict)`
- Produces: `load_connector_settings(config_dir: Path) -> dict[str, ConnectorSettings]`
- Produces: `CollectionResult(events: tuple[Event, ...], channel_names: dict[str, str])`
- Produces: `Connector` protocol with `name`, `validate`, and `collect`
- Produces: `Config.load(env=None, env_file=None)` where process values override the env file
- Produces: `Config.env_file: Path`, defaulting to `~/.config/teammem/hub.env`

- [ ] **Step 1: Write failing environment-file and connector-default tests**

```python
def test_process_environment_overrides_user_only_hub_env(tmp_path):
    env_file = tmp_path / "hub.env"
    env_file.write_text("TEAMMEM_GITHUB_TOKEN=file-token\nTEAMMEM_SINCE_DAYS=3\n")
    env_file.chmod(0o600)
    cfg = Config.load(
        env={"TEAMMEM_GITHUB_TOKEN": "process-token"},
        env_file=env_file,
    )
    assert cfg.github_token == "process-token"
    assert cfg.since_days == 3


def test_all_example_connectors_are_disabled(tmp_path):
    settings = load_connector_settings(tmp_path)
    assert set(settings) == {"github", "gitlab", "slack", "feishu", "discord"}
    assert not any(item.enabled for item in settings.values())
```

- [ ] **Step 2: Run the focused tests and confirm missing interfaces**

Run: `pytest -q tests/test_config.py tests/test_connector_config.py`

Expected: collection fails because `teammem.connectors` and new configuration fields do not exist.

- [ ] **Step 3: Implement strict env-file parsing and connector types**

Implement `read_env_file(path)` to accept blank lines, comments, and literal
`KEY=VALUE` pairs only. Reject group/world-readable files with a message that names
the path but never prints values. Merge defaults, file values, then process values.

```python
@dataclass(frozen=True)
class ConnectorSettings:
    name: str
    enabled: bool
    options: dict


@dataclass(frozen=True)
class CollectionResult:
    events: tuple[Event, ...] = ()
    channel_names: dict[str, str] = field(default_factory=dict)


class Connector(Protocol):
    name: str

    def validate(self, cfg: Config, settings: ConnectorSettings) -> list[str]: ...
    def collect(
        self, cfg: Config, ids: IdentityMaps, settings: ConnectorSettings,
        now: datetime,
    ) -> CollectionResult: ...
```

Add the resolved environment-file path, secret fields for GitHub, Slack, and
Discord, plus inbox/archive/quarantine and snapshot paths to `Config`. Keep
existing environment variable names unchanged.

- [ ] **Step 4: Add the disabled example configuration**

```yaml
connectors:
  github:
    enabled: false
  gitlab:
    enabled: false
  slack:
    enabled: false
  feishu:
    enabled: false
  discord:
    enabled: false
```

When `connectors.yaml` is absent, load the example fallback exactly as roster and
projects do. Unknown connector keys raise `ValueError`.

- [ ] **Step 5: Run configuration tests**

Run: `pytest -q tests/test_config.py tests/test_connector_config.py`

Expected: all tests pass, including `0600` enforcement and process-over-file precedence.

- [ ] **Step 6: Commit the configuration boundary**

```bash
git add config/connectors.example.yaml teammem/config.py teammem/connectors tests/test_config.py tests/test_connector_config.py
git commit -m "feat: add connector configuration boundary"
```

### Task 2: Provider-Namespaced Identity and Project Mapping

**Files:**
- Modify: `teammem/identity.py`
- Modify: `teammem/reclaim.py`
- Modify: `config/roster.example.yaml`
- Modify: `config/projects.example.yaml`
- Modify: `tests/fixtures/config/roster.example.yaml`
- Modify: `tests/fixtures/config/projects.example.yaml`
- Modify: `tests/test_identity.py`
- Modify: `tests/test_reclaim.py`

**Interfaces:**
- Consumes: connector names from Task 1
- Produces: `IdentityMaps.project(kind: str, value: str) -> str | None`
- Produces: `IdentityMaps.resources(kind: str) -> dict[str, str]`
- Preserves: `person`, `project_for_repo`, `project_for_channel`, and `mapped_channels`

- [ ] **Step 1: Write failing namespace and compatibility tests**

```python
def test_same_text_can_identify_resources_from_different_providers():
    ids = IdentityMaps(
        {"members": {"alex": {"github": ["alex-gh"], "slack": ["U1"]}}},
        {"projects": {
            "one": {"github_repos": ["same"]},
            "two": {"slack_channels": ["same"]},
        }},
    )
    assert ids.project("github-repo", "same") == "one"
    assert ids.project("slack-channel", "same") == "two"


def test_existing_gitlab_and_feishu_helpers_remain_compatible():
    ids = IdentityMaps.load(CONFIG_DIR)
    assert ids.project_for_repo("team/project-alpha") == "project-alpha"
    assert ids.project_for_channel("oc_example_alpha") == "project-alpha"
```

- [ ] **Step 2: Run the tests and verify the shared-map collision**

Run: `pytest -q tests/test_identity.py tests/test_reclaim.py`

Expected: namespace test fails because projects currently share one string map.

- [ ] **Step 3: Implement typed mappings**

Store project resources under `(kind, normalized_value)`. Load these additive
fields:

```python
RESOURCE_FIELDS = {
    "github_repos": "github-repo",
    "gitlab_repos": "gitlab-repo",
    "slack_channels": "slack-channel",
    "feishu_channels": "feishu-channel",
    "discord_channels": "discord-channel",
}
IDENTITY_FIELDS = ("email", "github", "gitlab", "slack", "feishu", "discord")
```

Keep old helpers as thin wrappers over typed keys. Generalize channel reclaim so
the event source determines the channel kind rather than assuming Feishu.

- [ ] **Step 4: Update public and test examples**

Add example GitHub, Slack, and Discord member IDs and project resources while
retaining current GitLab/Feishu examples.

- [ ] **Step 5: Run identity and reclaim tests**

Run: `pytest -q tests/test_identity.py tests/test_reclaim.py`

Expected: all tests pass and existing configuration remains valid.

- [ ] **Step 6: Commit typed mappings**

```bash
git add teammem/identity.py teammem/reclaim.py config tests/fixtures/config tests/test_identity.py tests/test_reclaim.py
git commit -m "feat: namespace connector identities and resources"
```

### Task 3: Registry and Existing GitLab/Feishu Adapters

**Files:**
- Create: `teammem/connectors/registry.py`
- Create: `teammem/connectors/gitlab.py`
- Create: `teammem/connectors/feishu.py`
- Modify: `teammem/gitlab_collector.py`
- Modify: `teammem/feishu_collector.py`
- Test: `tests/test_connector_registry.py`
- Modify: `tests/test_gitlab_collector.py`
- Modify: `tests/test_feishu_collector.py`

**Interfaces:**
- Consumes: `Connector`, `ConnectorSettings`, `CollectionResult`
- Produces: `connector_names() -> tuple[str, ...]`
- Produces: `get_connector(name: str) -> Connector`
- Produces: static registrations for all five official names

- [ ] **Step 1: Write failing registry and Feishu allowlist tests**

```python
def test_registry_lists_official_connectors_without_network():
    assert connector_names() == ("discord", "feishu", "github", "gitlab", "slack")


def test_feishu_fetches_only_project_mapped_channels():
    calls = []
    result = collect_feishu(cfg, ids, recording_fetch(calls), NOW)
    message_calls = [params for path, params in calls if path == "/im/v1/messages"]
    assert {call["container_id"] for call in message_calls} == {"oc_example_alpha"}
    assert all(event.source == "feishu-channel" for event in result)
```

- [ ] **Step 2: Run tests and confirm registry/allowlist failures**

Run: `pytest -q tests/test_connector_registry.py tests/test_gitlab_collector.py tests/test_feishu_collector.py`

Expected: registry import fails and Feishu still enumerates every bot-visible chat.

- [ ] **Step 3: Add registry declarations without import side effects**

Register lightweight connector objects. `validate` returns exact missing variable
names. `get_connector` raises `KeyError("unknown connector: <name>")`.

- [ ] **Step 4: Wrap GitLab without changing event identities**

Move HTTP construction and collection composition behind
`GitLabConnector`. Keep `collect_gitlab` import-compatible and preserve `source`,
`kind`, `refs`, and hashes byte-for-byte for existing fixtures.

- [ ] **Step 5: Restrict Feishu to configured channels**

Replace `/im/v1/chats` enumeration with iteration over
`ids.resources("feishu-channel")`. Fetch each configured channel's messages and,
when available, its display name. Return names in `CollectionResult`; do not write
`channel_names.json` inside the adapter.

- [ ] **Step 6: Run existing-adapter tests**

Run: `pytest -q tests/test_connector_registry.py tests/test_gitlab_collector.py tests/test_feishu_collector.py`

Expected: all pass; GitLab fixtures are unchanged and unconfigured Feishu channels are never requested.

- [ ] **Step 7: Commit registry and migrated adapters**

```bash
git add teammem/connectors teammem/gitlab_collector.py teammem/feishu_collector.py tests/test_connector_registry.py tests/test_gitlab_collector.py tests/test_feishu_collector.py
git commit -m "refactor: move existing collectors behind registry"
```

### Task 4: GitHub Adapter

**Files:**
- Create: `teammem/connectors/github.py`
- Test: `tests/test_github_connector.py`
- Modify: `teammem/connectors/registry.py`

**Interfaces:**
- Produces: `GitHubConnector`
- Uses: `GET /repos/{owner}/{repo}/commits`
- Uses: `GET /repos/{owner}/{repo}/pulls?state=all`
- Emits: `source="github"` and `kind in {"commit", "pr"}`

- [ ] **Step 1: Write fixture tests for commits, pull requests, pagination, and hashes**

```python
def test_github_normalizes_commit_and_pull_request():
    result = GitHubConnector(fetch=fixture_fetch).collect(cfg, ids, settings, NOW)
    assert [(e.source, e.kind) for e in result.events] == [
        ("github", "commit"),
        ("github", "pr"),
    ]
    assert result.events[0].person == "alex"
    assert result.events[1].refs == '{"number": 7, "url": "https://github.test/pull/7"}'
```

Also assert a second insertion produces zero new ledger rows and that commits use
SHA hashes while PR hashes include repository, PR number, state, and updated time.

- [ ] **Step 2: Run GitHub tests and confirm missing adapter**

Run: `pytest -q tests/test_github_connector.py`

Expected: import fails because `GitHubConnector` does not exist.

- [ ] **Step 3: Implement the REST adapter**

Use `Authorization: Bearer`, `Accept: application/vnd.github+json`, and the current
version header documented by GitHub. Request only configured `github_repos`;
paginate with `per_page=100`. Use the commit endpoint's `since` parameter and
filter pull requests by `updated_at` inside the lookback.

Minimum documented fine-grained permissions are Contents read for commits and Pull
requests read for PRs:

- <https://docs.github.com/en/rest/commits/commits>
- <https://docs.github.com/en/rest/pulls/pulls>

- [ ] **Step 4: Run adapter and idempotency tests**

Run: `pytest -q tests/test_github_connector.py tests/test_store.py`

Expected: all pass without network.

- [ ] **Step 5: Commit GitHub support**

```bash
git add teammem/connectors/github.py teammem/connectors/registry.py tests/test_github_connector.py
git commit -m "feat: add GitHub connector"
```

### Task 5: Slack and Discord Optional Chat Adapters

**Files:**
- Create: `teammem/connectors/slack.py`
- Create: `teammem/connectors/discord.py`
- Test: `tests/test_slack_connector.py`
- Test: `tests/test_discord_connector.py`
- Modify: `teammem/connectors/registry.py`

**Interfaces:**
- Produces: `SlackConnector`, `DiscordConnector`
- Slack emits: `source="slack-channel"`, `kind="message"`
- Discord emits: `source="discord-channel"`, `kind="message"`

- [ ] **Step 1: Write Slack privacy and limitation tests**

```python
def test_slack_queries_only_allowlisted_channels_and_skips_threads_and_bots():
    calls = []
    result = SlackConnector(fetch=slack_fixture(calls)).collect(cfg, ids, settings, NOW)
    assert {params["channel"] for path, params in calls} == {"C0123"}
    assert all(path == "conversations.history" for path, _ in calls)
    assert [event.summary for event in result.events] == ["human top-level"]
    assert not any("conversations.replies" in path for path, _ in calls)
```

Assert validation requires `TEAMMEM_SLACK_BOT_TOKEN`, not a user token. Use
`oldest`, cursor pagination, and a conservative page size compatible with Slack's
current non-Marketplace limits.

- [ ] **Step 2: Write Discord permission, allowlist, and bot exclusion tests**

```python
def test_discord_queries_only_mapped_channels_and_skips_bots_and_webhooks():
    calls = []
    result = DiscordConnector(fetch=discord_fixture(calls)).collect(cfg, ids, settings, NOW)
    assert {path for path, _ in calls} == {"/channels/9876543210/messages"}
    assert [event.person for event in result.events] == ["alex"]
```

- [ ] **Step 3: Run tests and confirm missing adapters**

Run: `pytest -q tests/test_slack_connector.py tests/test_discord_connector.py`

Expected: both imports fail.

- [ ] **Step 4: Implement Slack scheduled polling**

Call `conversations.history` only for `slack_channels` in project configuration.
Skip messages with `bot_id`, bot subtypes, missing `user`, or `thread_ts != ts`.
Use `ts` as the stable hash and include `channel_id` in refs. Do not call
`conversations.replies` and do not accept user tokens.

Official references:

- <https://api.slack.com/methods/conversations.history>
- <https://api.slack.com/methods/conversations.replies>

- [ ] **Step 5: Implement Discord scheduled polling**

Call `GET /channels/{channel_id}/messages?limit=100`, paging backward until the
lookback boundary. Require `VIEW_CHANNEL`, `READ_MESSAGE_HISTORY`, and message
content access. Skip `author.bot`, `webhook_id`, and non-content system messages.
Use message snowflake ID as the hash.

Official reference:

- <https://docs.discord.com/developers/resources/message#get-channel-messages>

- [ ] **Step 6: Run chat-adapter tests**

Run: `pytest -q tests/test_slack_connector.py tests/test_discord_connector.py`

Expected: all pass, with no request for an unconfigured channel.

- [ ] **Step 7: Commit optional public chat adapters**

```bash
git add teammem/connectors/slack.py teammem/connectors/discord.py teammem/connectors/registry.py tests/test_slack_connector.py tests/test_discord_connector.py
git commit -m "feat: add optional Slack and Discord connectors"
```

### Task 6: Reusable Hub Services and Portable Daily Workflow

**Files:**
- Create: `teammem/services.py`
- Create: `teammem/daily.py`
- Modify: `teammem/cli.py`
- Modify: `teammem/render.py`
- Modify: `teammem/queries.py`
- Test: `tests/test_services.py`
- Test: `tests/test_daily.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_render.py`

**Interfaces:**
- Produces: `collect_connector(name, cfg, ids, settings, now, dry_run=False)`
- Produces: `run_daily(cfg, ids, settings, now) -> DailyResult`
- Produces: `DailyResult(steps: tuple[StepResult, ...], exit_code: int)`
- CLI adds: `connectors list`, `connectors check`, `collect --enabled`, `run-daily`

- [ ] **Step 1: Write failing service-extraction regression tests**

Test that existing `journal`, `report`, `docs-sync`, and `render` output remains
unchanged after their bodies move from `cli.py` into callable service functions.

- [ ] **Step 2: Write failing daily isolation and ordering tests**

```python
def test_daily_continues_after_one_network_connector_fails(tmp_path):
    result = run_daily(cfg, ids, settings, NOW, connectors={
        "github": failing_connector("timeout"),
        "feishu": fixture_connector([EVENT]),
    })
    assert result.exit_code == 1
    assert result.status("github") == "failed"
    assert result.status("feishu") == "ok"
    assert result.status("render") == "ok"
```

Also assert a ledger-open failure stops reclaim/journal/report/render, Friday alone
runs the weekly report by default, unconfigured optional stages are marked
`skipped`, and channel display metadata is persisted atomically after successful
collection. With no LLM backend, journal/report are skipped and render succeeds;
an actual LLM call failure produces a non-zero result but still permits rendering
from ledger evidence and cached summaries.

- [ ] **Step 3: Run focused workflow tests**

Run: `pytest -q tests/test_services.py tests/test_daily.py tests/test_cli.py`

Expected: failures because callable services and daily workflow do not exist.

- [ ] **Step 4: Extract existing command services without behavior changes**

Move implementation, not CLI parsing, into focused functions. Keep `main()` as
argument parsing plus service invocation. Run the existing CLI suite after each
extraction.

- [ ] **Step 5: Implement the daily stage machine**

Use explicit `StepResult(name, status, detail)` values. Catch connector transport
errors per connector and continue. Treat ledger, reclaim, journal, report, and
render failures as visible failures, but distinguish dependencies: ledger/reclaim
failures skip dependent stages, synthesis failures still allow deterministic
rendering, and render failures stop vault commit/push. Import bundles only when all
three inbox paths are configured. Snapshot SQLite with
`sqlite3.Connection.backup()` into a dated file and retain 14 daily snapshots.

- [ ] **Step 6: Add registry-driven CLI commands**

```text
teammem connectors list
teammem connectors check
teammem collect github
teammem collect --enabled
teammem run-daily
```

`connectors list` must not authenticate. `connectors check` returns 2 for invalid
enabled configuration and never prints secrets. `run-daily` returns the
`DailyResult.exit_code`.

- [ ] **Step 7: Group GitHub PR and GitLab MR consistently**

Update work-kind filters from `("commit", "mr", "journal-highlight")` to
`("commit", "pr", "mr", "journal-highlight")`. Generalize message channel refs to
accept `channel_id` while retaining `chat_id` compatibility.

- [ ] **Step 8: Run workflow and render tests**

Run: `pytest -q tests/test_services.py tests/test_daily.py tests/test_cli.py tests/test_render.py tests/test_queries.py`

Expected: all pass.

- [ ] **Step 9: Commit the daily workflow**

```bash
git add teammem/services.py teammem/daily.py teammem/cli.py teammem/render.py teammem/queries.py tests/test_services.py tests/test_daily.py tests/test_cli.py tests/test_render.py
git commit -m "feat: add portable daily hub workflow"
```

### Task 7: Connector Documentation and Full Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/architecture.md`
- Modify: `docs/privacy.md`
- Modify: `docs/deployment.md`
- Modify: `pyproject.toml`
- Modify: `scripts/check-public.sh`
- Test: `tests/test_public_scan.py`

**Interfaces:**
- Documents: five official adapters and their exact visibility boundaries
- Documents: GitHub + Slack public quick start
- Preserves: Feishu as the private deployment's unchanged channel provider

- [ ] **Step 1: Update architecture and privacy documentation**

State that all network connectors are disabled by default, chat IDs are allowlisted,
Slack is top-level-only, DMs are excluded, and MemberKit remains the unsupported
source fallback.

- [ ] **Step 2: Add provider setup tables**

Document exact environment variables, non-secret YAML fields, and minimum current
permissions. Link to the official GitHub, Slack, Feishu, GitLab, and Discord API
pages used during implementation. Never include real tokens, hosts, group IDs, or
channel IDs.

- [ ] **Step 3: Run package and public-source checks**

Run:

```bash
pytest -q
python -m build
(cd packages/memberkit && python -m build)
./scripts/check-public.sh
```

Expected: all tests pass; both wheels and source distributions build; public scan passes.

- [ ] **Step 4: Run clean-install smoke tests**

Create temporary virtual environments, install each built wheel, and run:

```bash
teammem connectors list
teammem connectors check
teammem --help
memberkit --help
```

Expected: commands load without credentials, no network request occurs, and connector
status shows all disabled.

- [ ] **Step 5: Commit connector documentation and verification**

```bash
git add README.md docs pyproject.toml scripts/check-public.sh tests/test_public_scan.py
git commit -m "docs: document provider-neutral connectors"
```
