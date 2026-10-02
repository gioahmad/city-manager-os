# Unified workspace

Open `/workspace` using the existing private login. `/workspace/display` is the signed-in,
view-only PC/TV dashboard. It refreshes every minute and reports failed refreshes as stale data.
Use a READ_ONLY account on shared screens. The display API returns only existing alerts,
source health, work records, non-secret organization settings, and a refresh timestamp.
It does not query personal tables. Request links use a separate, narrowly scoped portal;
they do not provide access to the staff workspace.

## Release boundary

This release adds storage, routes, a responsive interface, and a dashboard-only installer.
It does not change source connectors, alert rules, spatial matching, notification routing,
recipient settings, or the existing Brain SMS webhook. Existing map time windows, watch
controls, contacts, work editing, attachment retrieval, and full search remain accessible.
It reuses existing `issues`, `brain_notes`, `contacts` references, private sessions, and
SMSGate sending. No Twilio, graph database, front-end build service, or background worker
is introduced.

Install from a clean, pinned checkout:

```sh
bash deploy/intelligence/install_intelligence.sh "$(git rev-parse HEAD)"
```

The installer backs up the database, applies additive 036/037 migrations, builds/tests the
dashboard, preflights login and table grants, and checks the authenticated workspace and
display projection. If the post-restart checks fail, it restores the previous dashboard
image. Test containers mount the deploy tree read-only so repository migration checks
use the same files as the release. Backup/install stage bars show completed stages;
long commands report elapsed time every five seconds, and the dump reports bytes
written. The stage bar is not a forecast of remaining time. It verifies that staff, ops, integration, and database container identities/images
did not change. New tables are retained on rollback, so no newly captured data is deleted.
Existing canonical contact tables must already be installed.

## Included behavior

| Request | Implementation |
| --- | --- |
| Unified Today | Existing work, private tasks, quick capture, due person reminders, health logging |
| Water/protein/fasting | Explicit mL/gram logs, optional personal goals, one active fasting session, history |
| Intelligence/TV | Existing alert/source records; 6h/12h/24h/7d/30d/All; read-only display refresh |
| Second brain | Existing private capture/search/attachments and a Markdown text export for Obsidian |
| People/places/projects | Named records and directional relationships, node/edge view, evidence inspector, navigable neighbors |
| Family inference | Aunt/uncle/parent's sibling, grandparent, and cousin derivations from confirmed facts |
| Suggestions | Shared surname, address, organization, phone, email, and tags; reasons and dismissals |
| Important dates | Owner-private annual/once dates, advance notice, daily/7-day/30-day briefings, handled state |
| Contextual text | Occasion-specific editable draft; saved verified recipient; explicit send via existing SMSGate |
| Outsider messages | Per-work-item 30-day capability link, public conversation, incoming messages, revocation |
| Product templates | Organization/product name, timezone, City/Business/Personal label, personal module visibility |

Dates surface when their reminder date reaches the selected briefing window. One-time
overdue dates remain until handled; annual events advance after the occasion. February 29
annual events use February 28 in non-leap years. Handled/SMSGate-accepted occurrences are
suppressed for that occurrence. These are in-app briefing reminders, not new scheduled
SMS or push notifications. No message is sent automatically.

## Relationship evidence and permissions

Private records belong to the signed-in username, including for Executive accounts. Work
records are shared with signed-in staff. Family edges are always private to their author.
An edge is visible only if its own permission and both endpoint records are visible.
Derivation operates on those permitted confirmed facts, never on uncertain suggestions;
it retains supporting fact IDs and recomputes after edits/removals. An aunt link and a
child do not determine a unique mother. Last names, common addresses, and shared phone
numbers never create family facts or silently merge identities.

Existing shared contacts can be explicitly linked into the directory. Private/executive-only
canonical contacts are not imported. Linked contacts retain their canonical contact ID;
edits happen in Contacts and become current when the import is refreshed. If the canonical
contact becomes inactive/private, workspace reads immediately exclude it and its paths.
The new module does not rewrite existing contact visibility policies.

Every important date and outbound message history belongs to its author, even when the
person is a shared work contact. Request portals expose only the deliberately shared work
title, status, and portal messages. Work descriptions, internal notes, people directories,
and personal data are not returned. Links are hashed at rest, expire after 30 days, can be
revoked, use CSRF tokens, and accept at most 20 requester messages per hour per link.

SMSGate acceptance is recorded separately from delivery. Draft IDs are claimed before the
external call, preventing a retry of the same draft from sending twice. If acceptance is
uncertain, the draft is not automatically retried; check the gateway before creating a new
draft. A successful gateway call can still precede a failed local status update; the retained
PENDING claim prevents duplicate sending. The sending path never changes existing webhook
registration or alert recipient routing.

## Current limits and next scope

The visible graph directory is bounded to 500 records, facts to 2,000, suggestions to 100,
work/alerts to 80, and Brain preview to 30. Original tools retain their full search and editing.
Candidate matching is deterministic; it does not crawl external profiles, invent calibrated
probabilities, or use AI to turn prose into confirmed facts. Aliases can be recorded but are
not currently a matching signal. Units are preserved; a matching building/address remains
a suggestion, not proof of a shared household. Graph exploration shows up to eight neighbors of the selected record, with solid confirmed
and dashed derived edges, plus a complete permitted evidence list. It is not a whole-directory
force-directed visualization. This release does not
provide full/half-sibling inference or editable custom inference rules.

Obsidian receives a Markdown text export; attachment downloads remain in Brain. There is
no bidirectional vault sync. Outside intake uses explicitly issued request links; SMS capture
keeps its existing behavior and is not silently converted into public tickets.

Start commercial customers in separate deployments with separate databases, credentials,
and backups, using the same codebase and configuration. Branding/templates do not provide
multi-tenant SaaS isolation, licensing/billing, or arbitrary per-customer workflow schemas.
Changing repository visibility remains a separate action; first arrange authenticated VPS pulls.

## Verification

Run the affected checks from `dashboard`:

```sh
python -m pytest -q tests/test_workspace.py tests/test_brain.py tests/test_global_share.py \
  tests/test_contact_directory_share.py tests/test_navigation_and_map_sharing.py \
  tests/test_executive_workflow.py tests/test_today_board.py
```

The workspace checks cover inference order/retraction/evidence, cousins/grandparents,
uncertain matching, leap-year recurrence, advance notice and handled dates, CSRF/read-only/
automation denial, personal query ownership, display projection, portal expiry/escaping,
Markdown ownership, and SMS claim/retry/uncertain acceptance handling. Existing module checks protect the surrounding integration paths.

Deployment command checks (no live Docker/database needed):

```sh
python -m unittest discover -s deploy/tests -p test_workspace_deploy.py -v
```

These exercise archive bytes/validation, failed-test stopping, read-only migration mounts,
stdin forwarding, elapsed/size reporting, dashboard-only restart, and a single rollback
after live verification failure.

## Navigation and workflow polish

All original navigation destinations are available in a searchable **All tools** menu on
both desktop and mobile. The phone rail opens explicitly instead of occupying the top of
every screen. Section links survive reload/back navigation through URL hashes. Today shows
upcoming private Outlook appointments, unfinished personal tasks by default, quick capture,
due relationship reminders, and optional health goals; advanced logging/goals collapse away.
Feed, work, and recent Brain filters operate locally without extra requests.

The client asks for the visible section only and renders that section only. Section data is
kept in page memory for up to 20 seconds; it is never placed in localStorage or a shared cache.
Successful writes clear section caches; explicit Refresh bypasses them. Superseded requests
are aborted/ignored. The server retains `view=all` for compatibility and applies the same
owner/endpoint checks to section requests. Private API responses are `no-store`.

The relationship view performs four database reads; Brain performs two; Work performs three;
Area intelligence and notices perform four. Those views do not query personal health/tasks.
Today includes twelve reads after adding the private Outlook snapshot/status. These are
query-count checks, not a claim about production latency. Relationship/identity matching
never runs when logging water or refreshing the alert feed. Date formatting is reused and
entity lookups use an index. Conversation previews retain the latest 100 messages per link;
Brain previews retain 1,200 characters with a link to full Brain. Full records remain intact.

See `docs/workspace-calendar.md` for the Microsoft setup, ownership boundaries, sync behavior,
and public town-feed onboarding. Microsoft credentials/account consent and verified town
feed URLs are required to turn new external connections on.

## Inbox and private knowledge intake

See [intelligence-today.md](intelligence-today.md) for the unified inbox, same-page source previews,
private context links, bulk document/data extraction, worker status, and optional local answers.
Microsoft 365 email/contact/calendar setup is in [workspace-calendar.md](workspace-calendar.md).
