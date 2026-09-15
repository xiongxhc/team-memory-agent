# Conversational TeamMem bot

The optional chat service answers ordinary conversation and retrieves cited team
facts from permitted projects. Its SQLite conversation state is separate from the
team ledger. It never calls collection, publication, or team-memory writes.

## Conversation behavior

| Conversation | Context | Trigger |
| --- | --- | --- |
| Direct message | Tenant + app + user | A message to the bot |
| Main group | Tenant + app + group | Explicit mention of this bot |
| Group thread | Tenant + app + group + thread root | Explicit mention in that thread |

A thread may use its recorded parent interaction, but does not inherit unrelated
main-group history. Group context includes speaker attribution. `/new` starts a
fresh context; `/forget` also clears retained chat content. A DM user controls their
own session; group/thread commands require an explicit group administrator grant.
Reset cancels queued work and suppresses late results. A message already accepted
by Feishu cannot be recalled by a local reset.

Both sender and group must appear in the access configuration. Grants use the
**new bot application's open IDs**, not another collector's IDs. The effective
project scope is the intersection of sender and group grants. Explicit `[]` allows
casual conversation with no project evidence. Removing an entry revokes access.
Project attribution in collector configuration does not grant chat access.

While an accepted request is being processed, the bot can show a temporary
`Typing` reaction on the original message. It does not send a separate thinking
message. Reactions are best-effort: a platform failure must not prevent an answer.

## Files

Authorized DMs can upload files or images. In groups, reply to a file message and
mention the bot; the service fetches that specific message and verifies its chat
and resource key. It does not collect general group history. Feishu may reject
external/restricted messages and inaccessible pre-join history. Card resources and
merged-forward submessages are unsupported.

Supported inputs: text/scanned PDF, PPTX including notes and rendered slides,
legacy PPT conversion, DOCX, XLSX, inert HTML, CSV, TXT, Markdown, PNG and JPEG.
Text fragments cite page, slide, sheet/cell or line. The model also receives a
bounded selection of rendered images. OCR and visual interpretation can be
imperfect; omitted/truncated content is disclosed instead of implying full coverage.

Defaults: 30 MiB per file, 60 MiB per request, five files per request, 200 pages or
slides per file, 20 visual pages per request, and a 60-second parsing deadline.
Archives, decoded image pixels, extracted text, output size and process resources
are bounded. Parsing runs through Linux bubblewrap with no network, credentials,
host home directory, scripts or macros. If isolation is unavailable, parsing fails.
Encrypted and unsupported files receive a clear explanation.

Raw files and rendered/conversion artifacts expire after 24 hours. Later visual
questions may require reattachment; expired files are not silently downloaded again.
Extracted text and OCR context last for the session, subject
to idle expiry (30 days by default), reset and forget. Neither is added to the
shared ledger or vault. Cleanup is logical application deletion, not a promise of
forensic erasure from disks or backups.

## Setup

Install the optional packages into a dedicated environment:

```sh
python -m pip install -e '.[chat,chat-documents]'
```

Use [chat.example.json](chat.example.json) as a complete disabled template. Keep
real tenant/app/user/group identities and model selection in private configuration.
Create a dedicated mode-600 env file with:

```text
TEAMMEM_CHAT_FEISHU_APP_ID=cli_exampleapp123
TEAMMEM_CHAT_FEISHU_APP_SECRET=
OPENAI_API_KEY=
```

Choose one explicit `model.provider`:

- `codex_cli`: uses the service user's existing Codex ChatGPT login. Run `codex login`
  as that user first; no API key is needed. Calls are ephemeral, use a temporary
  working directory and disable native tools, inherited configuration and rules.
  TeamMem owns the bounded search loop and conversation state. Account usage limits
  are shared with other services using that login.
- `openai_responses`: requires `OPENAI_API_KEY` in the dedicated env file and uses
  streaming Responses requests with `store=false`.

Both routes use the privately configured model, a shared 45-second model deadline,
at most three requests and two read-only retrieval rounds. There is no provider or
model fallback. Codex CLI output is bounded after generation and by the deadline;
the API additionally enforces its requested output-token limit. Codex ephemeral
execution prevents local session rollouts; it does not promise zero provider retention.

Install bubblewrap, LibreOffice, Tesseract with English and Simplified Chinese
language data, and suitable CJK fonts. Alternatively build the isolated document
runtime using `scripts/chat-document-runtime.Dockerfile`, export its root filesystem
to a user-owned directory, and set `paths.document_runtime_root`. The host still
needs functional native bubblewrap. The bundle does not require changing the host's
system packages or weakening its namespace policy. Keep a record of the built image
digest and package versions for reproducibility and updates.

The Feishu app needs Bot capability and these tenant scopes:

- `im:message.group_at_msg:readonly`
- `im:message.p2p_msg:readonly`
- `im:message:send_as_bot`
- `im:message:readonly`

For the temporary working reaction, also grant `im:message.reactions:write_only`
(add and remove reactions). It is optional for answering messages; missing reaction
permission disables the indicator rather than failing a conversation.

Publish scope changes. Configure a long connection and the
`im.message.receive_v1` event with the running service. Verify the bot open ID
using bot info, and establish trusted tenant/user/group mappings before enabling
access. Keep the service disabled until all checks and actual chat/file tests pass.

```sh
teammem chat check --config /path/to/private/chat.json
teammem chat serve --config /path/to/private/chat.json
```

Readiness reports no secrets. It checks configuration, credentials, scopes, SDK,
ledger read access, separate writable state, document runtime and actual bot
identity. `serve` refuses failed readiness. Readiness does not establish semantic
model quality or successful message/file delivery; test those separately.

## Team knowledge retrieval

Search ranks topic keywords and phrases instead of requiring the entire question
to appear as one substring. It supports Chinese terms and common date spellings.
Matching GitLab issues and merge requests prefer their latest authorized update within any
requested time/person filters, so an older failure report does not displace its
later resolution merely because it repeats more query words.

The ledger is opened read-only. Detailed project grants allow short event summaries
and validated original Feishu text, GitLab comments, and commit messages from the
same event. Arbitrary raw metadata and unrelated issue descriptions are not passed
to the model. Count-only project grants return commit aggregates only. Results
remain bounded to eight excerpts; each search has a five-second SQL deadline.

Person/day and team/week cached summaries have no complete project-level source
provenance, so chat does not expose them or infer access from their headings. The
generated vault is not independently searched. Missing or incomplete search
results do not prove that a fact or plan does not exist.

## Delivery and recovery

Events are committed before SDK acknowledgement. One session processes one turn
at a time, with at most two concurrent model calls across sessions. Replies have
persisted UUIDs and recheck authorization/generation before delivery. Feishu
[documents a one-hour UUID deduplication window](https://open.feishu.cn/document/server-docs/im-v1/message/reply).
Retries preserve the UUID and content; old ambiguous replies are quarantined
instead of risking a duplicate beyond the window. An interrupted request may need
the user to resend rather than the service silently replaying an obsolete question.

## Verification

```sh
python -m pip install -e '.[dev,chat,chat-documents,chat-test]'
python -m pytest -q tests --ignore=tests/test_memberkit_integration.py
python scripts/eval-chat.py
python scripts/eval-chat.py --live --config /path/to/private/chat.json --output evaluation.json
```

The 46-case model evaluation uses synthetic team evidence, extracted file context,
and generated images only; it sends no Feishu messages and opens no team ledger.
Its automatic citation/leak checks require human review for answer quality. Parser
and live Feishu file acceptance are distinct verification steps. A fixture-only
run is not a live model evaluation.

Stop only the chat process to roll back. Preserve its private state and restore its
previous reviewed engine/configuration; leave collection and publication running.
