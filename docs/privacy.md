# Privacy and consent

## Default state

Installing `teammem` performs no provider request, enables no connector, and
creates no schedule. Every GitHub, GitLab, Slack, Feishu, and Discord enable flag
defaults to `false`. `teammem connectors list` and `teammem connectors check`
inspect local configuration only.

The hub belongs on an always-on, operator-controlled Mac mini, Linux server,
VPS, or Windows machine. Windows scheduling is current-user and logged-in-only:
a screen lock is supported, while logout prevents future runs. Provider payloads
and the SQLite ledger remain on that machine.
Rendered views expose normalized summaries and references; operators are
responsible for access control to the ledger, inbox, archive, quarantine,
snapshots, and rendered views.

## Central provider visibility

The operator must make the integration's collection boundary visible to the team.
Forge tokens should be restricted at the provider. Chat apps must be visibly
present in each collected public/private project channel or group/guild channel.

| Connector | Can see through Team Memory Agent | Deliberately excluded |
|---|---|---|
| GitHub | Default-branch commits and updated pull requests in explicitly mapped repositories | Unmapped repositories |
| GitLab | Commits, merge requests, issue lifecycle observations, and repository creations in the operator-configured group hierarchy and subgroups; polling captures one initial-creation fact and one provider-reported closure fact per issue; known repositories receive project attribution | Projects outside the configured hierarchy, projects merely shared into it, issue/commit comments, and repeated reopen/reclose history (which requires a state-events or webhook source) |
| Slack | Human top-level messages in explicitly mapped public or private project channels containing the app | DMs, multi-person DMs, unlisted channels, thread replies, bot messages |
| Feishu | Human messages in explicitly mapped group chats containing the app | Direct chats, unlisted group chats, non-user senders |
| Discord | Human content messages in explicitly mapped guild channels visible to the bot | DMs/group DMs, unlisted guild channels, bot and webhook messages |

Slack uses a bot token, never a user token. It checks channel metadata before
history, fails closed when metadata is unavailable, never calls
`conversations.replies`, and polls history in 15-message pages with global
60-second pacing across pages and channels. Slack's tighter limit applies to
affected commercially distributed apps outside Marketplace approval; Slack says
internal customer-built apps are not affected. The adapter nevertheless uses
the conservative behavior for portable deployments and treats `Retry-After` as
authoritative.

Discord checks channel metadata for a guild ID before requesting messages. An
empty result is not proof that the channel is empty: missing
`READ_MESSAGE_HISTORY` can return no history, and missing `MESSAGE_CONTENT` can
hide user-authored content. The daily result surfaces that diagnostic.

Feishu is an official option, not a legacy path. The existing private deployment
remains Feishu-based and unchanged; enabling Slack in a separate public
deployment does not migrate, replace, or reconfigure it. Public Slack remains
optional and collects only human top-level messages in its allowlisted channels.

## Credentials and configuration

Non-secret enable flags and allowlisted resources live in operator-owned YAML.
On macOS and Linux, tokens and app secrets live in the process environment or
`~/.config/teammem/hub.env`, which must have mode `0600`. On Windows, the default
is `%APPDATA%\\TeamMemory\\hub.env`; it must be a current-user-owned regular,
non-reparse-point file with no shared-principal read allow rule. Process values
override the file. The environment-file parser treats values literally, so
operators use absolute paths and do not rely on shell expansion.

Validation reports missing environment-variable names, never values. Connector
and daily-run errors redact configured credentials. Credentials, runtime
databases, inbox exports, archives, quarantine files, snapshots, and generated
views must never be committed to this repository.

## Member-owned data

MemberKit opens its configured observations database read-only. It creates a
local bundle containing one-line structured highlights and a derived Markdown
preview. The member can remove any event before pushing. The preview is
regenerated from the reviewed event list during review and again before push.

The default draft emits every eligible short frozen-v1 observation projection in
timestamp order, with no scoring, consolidation, semantic deduplication,
per-project cap, LLM call, or network call. A busy day can contain hundreds of
events. `memberkit draft --all` is retained only as a compatibility alias and
produces the same event set.

The frozen `teammem-bundle/v1` shape is not a content-safety guarantee. MemberKit
does not read or emit the observation database's internal `facts` column and
never serializes raw database rows, session identifiers, source metadata, files,
or complete narratives. Eligible title or bounded narrative summaries may still
contain sensitive text or local references. The member must review and redact
every event before push. Member review remains the final privacy boundary. Local
review state remembers approved and excluded event fingerprints so later runs
cannot silently restore a redacted event.

Member drafts, review state, and the inbox clone remain under the configured
MemberKit work directory (`~/.memberkit` by default). Private configuration is
stored separately at `~/.config/teammem/memberkit.env` with user-only
permissions. Uninstalling the package does not delete these member-owned files.

## Manual fallback for unsupported sources

For WhatsApp, Telegram, LINE, email, a meeting, or another unsupported source, a
member may add a valid `journal-highlight` object to an existing local draft's
`events` list. It remains editable and reviewable and is transmitted only by the
separate `memberkit push` action. MemberKit does not scrape, authenticate to, or
automatically read any unsupported application.

Bundle v1 deliberately has no structured origin field. A member may put an
origin label in the human-readable summary; the hub records the accepted event as
`source=bundle:<member>`.

## Scheduling

The optional MemberKit schedule drafts and reminds. It never imports the push
module, invokes Git, commits, or transmits. Repeated reminders continue for every
pending date until it is reviewed or dismissed. A malformed or partially edited
existing draft is left byte-for-byte untouched and remains in the reminder list.
Valid and manually edited drafts are also never overwritten automatically. Only
the member's explicit `memberkit draft --force` action can replace an existing
draft; `--all` does not imply `--force`.

Package installation alone does not create a MemberKit schedule. `memberkit
setup` asks the member to accept the proposed 17:30 time in the Mac's local
timezone, choose another time, or decline. `MEMBERKIT_TIMEZONE` controls
member-calendar attribution after a run starts; it does not move that launchd
trigger.

The hub's `teammem run-daily` command also runs once and never installs a
schedule. Package installation creates no background job. Only the explicit
`teammem schedule install --time HH:MM` command installs or replaces the
operator's 18:20-local-time-by-default launchd, systemd user, or Windows Task
Scheduler job; `teammem schedule status` inspects it and `teammem schedule
remove` removes it.

The schedule definition contains no credential values. Its process invocation
contains only the resolved executable, `--env-file`, the private environment
file's path, and `run-daily`; secrets stay inside the separately protected
environment file. On Windows, the XML additionally contains the current-user
SID and paths only: it contains no provider, Git, or Windows credential, no
password, and no secret values. The task directly launches `teammem.exe`; there
is no PowerShell, `cmd.exe`, or shell wrapper. The built-in job does not pull or
export the private MemberKit Git inbox. Operators either refresh a disposable
staging export separately before a scheduled import or omit inbox paths from the
scheduled configuration.

Scheduling does not turn the hub into an inbound service. Provider polling and
operator-owned Git transport need outbound access, but the hub opens no inbound
public port.

## Intended use

The system records evidence for journals and team/project reports. It does not
rank people, calculate performance scores, or infer employee quality.
