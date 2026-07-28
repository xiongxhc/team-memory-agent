# Privacy and consent

## Default state

Installing `teammem` performs no provider request, enables no connector, and
creates no schedule. Every GitHub, GitLab, Slack, Feishu, and Discord enable flag
defaults to `false`. `teammem connectors list` and `teammem connectors check`
inspect local configuration only.

The hub belongs on an operator-controlled, normally available Mac mini, Linux
server, or VPS. Provider payloads and the SQLite ledger remain on that machine.
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
| GitLab | Commits and merge requests in the operator-configured group hierarchy and subgroups; known repositories receive project attribution | Projects outside the configured hierarchy and projects merely shared into it |
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
deployment does not migrate, replace, or reconfigure it.

## Credentials and configuration

Non-secret enable flags and allowlisted resources live in operator-owned YAML.
Tokens and app secrets live in the process environment or
`~/.config/teammem/hub.env`, which must have mode `0600`. Process values override
the file. The environment-file parser treats values literally, so operators use
absolute paths and do not rely on shell expansion.

Validation reports missing environment-variable names, never values. Connector
and daily-run errors redact configured credentials. Credentials, runtime
databases, inbox exports, archives, quarantine files, snapshots, and generated
views must never be committed to this repository.

## Member-owned data

MemberKit opens its configured observations database read-only. It creates a
local bundle containing one-line structured highlights and a derived Markdown
preview. The member can remove any event before pushing. The preview is
regenerated from the reviewed event list at push time.

MemberKit does not include raw observations, database rows, local files, direct
messages, or credentials. Local review state remembers approved and excluded
event fingerprints so catch-up runs cannot silently restore a redacted event.

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

Package installation alone does not create a MemberKit schedule. `memberkit
setup` asks the member to accept the proposed 17:30 local time, choose another
time, or decline.

The hub's `teammem run-daily` command also runs once and never installs a
schedule. Built-in hub schedule installation is not part of the current command
set.

## Intended use

The system records evidence for journals and team/project reports. It does not
rank people, calculate performance scores, or infer employee quality.
