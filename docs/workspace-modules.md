# Workspace module controls

Open **System → Modules** to turn optional workspace tools on or off for everyone.
Only an Executive can change switches; other signed-in accounts can view them.
All tools default to On. Switches persist in `workspace_config.settings.modules`
and take effect on the next request without a restart or database migration.

The first set covers Brain, Mapping (including Flood), Events, Event Intelligence,
Transit, Places / References, Staff, Today Board and Routines. Off removes the tool
from the shared menu and command palette, including recently opened links. Its
direct page/API routes and form submissions return `423` with recovery guidance.
Shared Intake actions cannot create Brain notes or Events while those tools are off.
The Modules page remains available so an Executive can turn a tool back on.

These are workspace access controls. Ingestion, existing records, the employee
portal, automation requests and live Watch matching/delivery continue running.
Existing records remain available to shared search and context views. Alerts,
Watches, recipients, system settings, Email, Calendar and Contacts remain available.
Feed activation and individual Watch on/off controls remain in their existing tools.

`python ci/check_workspace_modules.py` checks real settings updates and the actual
application middleware/routes on the CI-local PostgreSQL database, plus native
forms, command history, saved-link recovery and mobile width in Firefox and
Chromium. No external message is sent. The existing full release checks also run.

## Deploy and remaining live check

This release includes the prior mapped-activity and global Watch controls. Install
the dashboard plus the central matcher, resolved/mapped rematch and ntfy formatter
with their existing reviewed-release installers. A dashboard-only install does not
update the n8n notification path.

After installation, verify the TMD Watch's two recipient routes, current matched
records and notification history on the VPS. CI verifies matching, audit storage,
deduplication and delivery reservations; it does not establish that the live
recipients received a push. Historical `2 matches / 0 sent` is still a live
verification item until that output is inspected.
