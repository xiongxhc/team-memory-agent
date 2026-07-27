# Privacy and consent

## Member-owned data

MemberKit opens its configured observations database read-only. It creates a local
bundle containing one-line structured highlights and a derived Markdown preview.
The member can remove any event before pushing. The preview is regenerated from
the reviewed event list at push time.

MemberKit does not include raw observations, database rows, local files, direct
messages, or credentials. Local review state remembers approved and excluded event
fingerprints so catch-up runs cannot silently restore a redacted event.

## Scheduling

The optional schedule drafts and reminds. It never imports the push module, invokes
Git, commits, or transmits. Repeated reminders continue until pending dates are
reviewed or dismissed.

## Central signals

Forge activity is already visible to the configured organization. Shared-channel
collection requires an integration that is visibly present in each configured
channel. Direct and private channels are outside the product boundary.

## Intended use

The system records evidence for journals and team/project reports. It does not rank
people, calculate performance scores, or infer employee quality. Operators remain
responsible for access control to the ledger, inbox, archive, and rendered views.
