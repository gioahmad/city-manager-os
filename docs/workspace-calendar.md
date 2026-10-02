# Private Microsoft 365 connector

Our own connector imports email, contacts, and calendar events directly from Microsoft Graph.
Each workspace login connects one Microsoft account with delegated **read** access. It imports
up to 250 emails from **Inbox in the last 30 days**, up to 500 contacts from the **default contacts
folder**, and occurrences from the **primary calendar in the next 30 days**. These are bounded
working snapshots, not a complete mailbox backup. Message text is limited to 20,000 characters;
email attachments, shared mailboxes, additional folders/calendars, and Microsoft writes are not
included in this release. Use Open in Outlook for the full message or event.

Connecting performs the first sync. The private intake worker refreshes connected accounts
approximately every 15 minutes. Settings offers Refresh, Reconnect, and Disconnect. Existing
calendar-only connections retain their old consent and continue to sync; select Reconnect once
to approve the new email/contact scopes. Import failures retain the previous successful snapshot.

## One-time Microsoft and server setup

1. Open [Microsoft Entra](https://entra.microsoft.com/) → App registrations → New registration.
   For your own Office 365 tenant, use the single-tenant audience and its Directory/tenant ID.
   Register the **Web** redirect URI (not SPA):
   `https://YOUR-DASHBOARD-DOMAIN/workspace/calendar/microsoft/callback`.
   An existing app registration used by our calendar connector can be reused.
2. API permissions → Microsoft Graph → **Delegated permissions**: add `Mail.Read`, `Contacts.Read`,
   `Calendars.ReadBasic`, and `offline_access`. Remove a default `User.Read` if this app is only
   used by this connector. Tenant consent policy may require an administrator to grant consent.
   No application-wide mailbox access, mail-send, or Microsoft-write permission is requested.
3. Certificates & secrets → New client secret. Copy the **secret Value**, not its ID.
4. Edit `/opt/city-manager-os/dashboard/.env` on the VPS. Preserve the other settings and keep
   exactly one value for each key below; reuse the existing origin and encryption key if present:

   ```dotenv
   CMOS_PUBLIC_ORIGIN=https://YOUR-DASHBOARD-DOMAIN
   CMOS_MICROSOFT_CLIENT_ID=YOUR-APP-CLIENT-ID
   CMOS_MICROSOFT_CLIENT_SECRET=YOUR-SECRET-VALUE
   CMOS_MICROSOFT_TENANT=YOUR-DIRECTORY-TENANT-ID
   CMOS_CALENDAR_KEY=YOUR-GENERATED-KEY
   ```

   Generate a key only if one does not already exist:

   ```sh
   python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
   ```

   Store credentials only on the server. Back up the encryption key separately from database
   dumps. Changing it requires reconnecting accounts. Rotate client secrets before they expire.
5. Run the pinned `deploy/intelligence/install_intelligence.sh` release command. This applies
   additive migrations, validates a database backup, tests and restarts the dashboard, and starts
   the separate private intake worker. Existing staff and alert/integration engines keep running.
   If configuring credentials after installation, run the same pinned installer again so both
   dashboard and worker receive the new environment.
6. Open Workspace → Settings → Microsoft 365 → **Connect Microsoft 365**. Sign in to the intended
   Office 365 account once. Open **Inbox**, select Microsoft email or Microsoft contacts, and review
   the imports. Select **Import as private person** to add a contact to your People directory;
   a valid birthday creates a private annual reminder. Unusual phone formats remain available in
   the source preview for manual review. Confirm a number before using the existing SMS action.

## Ownership and failure behavior

Tokens and PKCE verifiers use authenticated Fernet encryption. OAuth state is random, hashed at
rest, bound to the owner and signed session, expires in ten minutes, and is consumed once. The
callback needs the same signed-in workspace session. Token and Graph redirects are never
followed; pagination can send credentials only to `https://graph.microsoft.com/v1.0/`.
Connection APIs return readiness, counts, and timestamps, never tokens or client secrets.

Every email/contact/calendar read is scoped to the workspace owner. TV views return before
private queries; outside request portals have no access to the inbox or imports. A context link
is private to its creator and never changes the visibility of the source or linked record.
Contact import creates a separate PRIVATE workspace person; it does not overwrite canonical
contacts, notification recipients, watches, or existing alert rules. Repeated import of the same
Microsoft contact is idempotent. Later Microsoft refreshes update the import preview while
preserving your edited People record. Family connections still require confirmed source facts.

All three snapshots are normalized before they replace existing data. A provider error or
failed page cannot publish a partial snapshot; rotated refresh tokens are preserved even if
subsequent Graph reads fail. Email/contact provider keys are upserted so local IDs and context
links survive refresh. Emails with confirmed context links and contact previews you imported into
People are retained outside the current working window, so follow-ups keep their provenance;
counts can include these retained sources. Disconnect removes local imported email, contact previews, and calendar
events. People, birthday reminders, and follow-up tasks you explicitly created remain. Microsoft
records are unchanged; revoke Microsoft consent separately in your Microsoft account if needed.

## Public town notice and event feeds

Workspace Settings links directly to the existing source onboarding wizard with RSS or ICS
selected. Only **public** town/organization feeds belong there; personal calendar links belong
to the private connection above. The wizard retains endpoint/auth checks, test-before-activate,
health tracking, and polling controls. Adding a source does not reconfigure existing connectors,
watch thresholds, recipients, or delivery routing.

Area intelligence now shows existing normalized regional notices/events and lets the user
filter both alerts and notices by town. No guessed feeds are seeded, no town is represented
as connected without a configured source, and no live source is activated by this release.
A town that offers only web pages or email notices still needs a verified adapter/feed path.

References:
- https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow
- https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-list-contacts?view=graph-rest-1.0
