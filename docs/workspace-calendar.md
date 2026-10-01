# Private Outlook / Microsoft 365 calendar

This deployment supports an owner-private, read-only connection to one Microsoft account's
primary calendar. Microsoft calendarView supplies individual occurrences in the next 30 days,
including recurring meetings. Connecting performs an initial sync; Settings has Refresh,
Reconnect, and Disconnect controls. Sync is explicit, not a new background poller.
Google/iCloud and additional Microsoft calendars are not connected by this release.

## One-time server setup

1. In Microsoft Entra, register an application with a **Web** redirect URI:
   `https://YOUR-DASHBOARD-DOMAIN/workspace/calendar/microsoft/callback`.
2. Select the account/tenant audience appropriate to your organization. The default tenant
   is `organizations`; a specific tenant GUID can restrict the application to your directory.
3. Add delegated Microsoft Graph `Calendars.ReadBasic` permission and `offline_access`.
   Tenant policy may require administrator consent. No mail or calendar-write permission is used.
4. Create a client secret. Put the **secret value**, app/client ID, and tenant in
   `/opt/city-manager-os/dashboard/.env`, preserving all existing values:

   ```dotenv
   CMOS_PUBLIC_ORIGIN=https://YOUR-DASHBOARD-DOMAIN
   CMOS_MICROSOFT_CLIENT_ID=YOUR-APP-CLIENT-ID
   CMOS_MICROSOFT_CLIENT_SECRET=YOUR-SECRET-VALUE
   CMOS_MICROSOFT_TENANT=organizations
   CMOS_CALENDAR_KEY=YOUR-GENERATED-KEY
   ```

   Generate the key on the VPS using Python's standard library:

   ```sh
   python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
   ```

   Keep the key private and back it up separately from database dumps. Tokens and PKCE
   verifiers are encrypted using authenticated Fernet encryption with this key. Losing or
   changing it requires reconnecting Microsoft accounts. Rotate client secrets before expiry.
5. Run the pinned workspace installer after the environment is configured. It applies the
   additive 038 migration and builds the encryption dependency into the dashboard image.
6. In Workspace → Settings → Outlook, select **Connect Outlook**, sign in to the intended
   Microsoft account, and approve read access. Each workspace login connects its own account.

State is random, hashed at rest, bound to the current owner/session, expires after ten minutes,
and is consumed once. PKCE protects the code exchange. Refresh credentials stay on the server;
state APIs return only connection readiness and status, never tokens or client secrets.
The callback requires the same signed-in workspace account. The existing private session
cookie must be sent on Microsoft's top-level callback; cross-site cookie/proxy policies must
allow this standard redirect. Token/Graph redirects are not followed. Pagination destinations
are restricted to Microsoft's Graph host and capped at 1,000 events / ten pages.

Appointments are queried by owner. The TV API returns before any calendar queries; outsider
request links cannot read appointments. Imported calendars are not copied to operational
work, shared event intelligence, or notification recipients. Disconnect deletes the local
connection and its imported events; it does not change Microsoft events. Microsoft consent
can additionally be revoked in the user's account settings.

If refresh fails, the previous snapshot remains and the interface marks it stale. A rotated
refresh token is retained even when the subsequent event fetch fails. No partial event page
replaces a complete snapshot. The connector stores only event title/time/location and the
Outlook link, not bodies, attachments, attendee lists, or mail. All-day labels are shown as
all-day; timed appointments use the workspace timezone.

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
- https://learn.microsoft.com/en-us/graph/api/user-list-calendarview?view=graph-rest-1.0
