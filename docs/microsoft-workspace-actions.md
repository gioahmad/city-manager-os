# Email, Calendar and Important — Microsoft workspace actions

This release adds `/email`, `/calendar`, and `/important` inside the shared City Manager OS navigation. It does not replace the internal Events Center or change alert routing, watches, recipients, SMSGate, n8n workflows, or the privacy boundary for outside request portals / TV displays.

## Reading and selective internal capture

Email shows the existing owner-private working snapshot: up to 250 Inbox messages from the last 30 days, plus linked or Important sources retained outside that window. It is not a full mailbox archive. Other folders, shared mailboxes and attachments remain in Outlook. Sender/subject/body search is supported. Calendar shows agenda, day, week and month views for calendars owned by the connected mailbox. It refreshes the selected window (up to a 42-day grid), stores stable local source IDs and explicitly identifies retained, potentially incomplete data when Microsoft is unavailable. Shared/delegated calendars are intentionally excluded. Recurring and all-day appointments can be displayed; this release does not edit recurring series or create all-day appointments.

An Important flag is private to the signed-in workspace owner and does not alter Microsoft. Add to my system provides reviewed destinations: private follow-up, shared Command Center work, shared internal event, private Brain note, or private link to an accessible existing record. Copying into Work/Events requires explicit acknowledgement of shared visibility. Original sources and confirmed links are preserved. The saved result includes an exact destination link. A database receipt and transaction-scoped locking protect retries and simultaneous submissions. Existing Work/Event promotion reuses an already-linked target instead of silently duplicating it.

Internal and Microsoft actions are separate. A saved Work/Event can be followed by an explicitly reviewed Add to Outlook action; Microsoft failure does not undo an internal success. This is selective read plus explicit write, not automatic bidirectional synchronization or conflict resolution. Later edits in City Manager do not silently overwrite Outlook.

## Optional Microsoft write consent

Existing connections and normal Connect keep delegated read-only scopes. To enable actions, add the following **delegated** Microsoft Graph permissions to the existing Entra Web app registration:

- `Mail.Send`: explicitly confirmed new mail and replies.
- `Mail.ReadWrite`: Outlook drafts, including reply drafts. The granted permission is broader than draft creation; the UI does not expose mailbox delete/move actions.
- `Calendars.ReadWrite`: appointments in a selected owned, editable calendar.

Keep the existing `Mail.Read`, `Contacts.Read`, `Calendars.ReadBasic`, and `offline_access` permissions, client ID, client secret, tenant, encryption key, and registered callback `/workspace/calendar/microsoft/callback`. Do not rotate the encryption key or disconnect merely to enable writes. A tenant administrator may need to consent under the organization's policy.

Open the app through its configured public HTTPS origin, sign in there, and choose **Enable Microsoft actions** on Email/Calendar. Review the permission explanation and continue to Microsoft. OAuth state and PKCE remain bound to that signed-in session; requested scopes are stored with the pending authorization. Do not begin from an unrelated HTTP/Tailscale origin and expect its login cookie to exist on the HTTPS callback domain. Imported sources are pinned to the verified mailbox identity; choosing a different account is rejected unless the user deliberately disconnects first. Disconnecting removes imported snapshots, cancels unsubmitted reviews, and leaves previously created internal records and action receipts.

No live credentials are included in this code or its tests. Existing read access continues to work without approving write permissions. A successful code deployment alone does not prove the live mailbox has consent, a valid secret, or a successful current sync.

## Reviewed sending and appointment creation

New email, Reply, and **Forward text** open a composer. Forward text copies only reviewed text, not original attachments or full MIME. Reply recipients are read from Microsoft's `replyTo` (or sender when absent) during preparation and displayed before final confirmation. To/CC/BCC, account, subject, body, target calendar, dates, time zone, and attendee/invitation intent are shown as applicable.

Preparation makes **no Graph content write**. It stores an owner-private review with an encrypted executable payload and a 30-minute expiration. Only the owner can confirm that stored operation; the confirmation endpoint cannot substitute a different message. Calendar writes use a transaction ID. Exact sender-account and calendar editability checks are repeated at execution. Local dates are interpreted in the selected IANA time zone; DST gaps/overlaps are rejected unless an unambiguous offset/UTC time is supplied.

A durable RUNNING claim is committed before the Microsoft POST. The same operation is not replayed after concurrent confirmation, timeout, process interruption or response loss. An ambiguous result is UNKNOWN (an interrupted claim can remain RUNNING); check Outlook before deliberately preparing a new operation. No automatic sender or retry queue is installed. A definite rejection is FAILED. Microsoft `202 Accepted` is reported as accepted for sending, **not delivered**. A draft creation is reported separately as **not sent**. Attendees require an explicit invitation choice and confirmation; an appointment with no attendees sends no invitations.

Receipt/history data is owner-scoped. The executable Microsoft request payload is encrypted with the existing calendar key; review text and outcome metadata are stored in the local private database. Retain and protect backups accordingly.

## Verification

The new workflow runs the existing regression suite, browser/navigation checks, a Docker build and route checks. Additional tests exercise actual SQL storage using selected unmodified foundational migrations plus migration 042 on isolated PostGIS 17.3.5, with Microsoft HTTP replaced by a strict mock transport. These include save/reopen, atomic receipts, source-link persistence, ownership/CSRF/read-only checks, flag retention, additional-calendar preservation, stale read results, scoped consent, reviewed sending/drafts/invitations, account changes and ambiguous outcomes. Firefox and Chromium use the real new templates, handlers, security middleware and test database. No production host, real mailbox, live notification system, or actual Microsoft invitation is contacted.

A controlled live acceptance after deployment/consent is still required before claiming Microsoft delivery or the user's real calendar integration works end-to-end. Backup and migration safety gates remain enabled.

References:
- https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-post-messages?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/message-reply?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/message-createreply?view=graph-rest-1.0
- https://learn.microsoft.com/en-us/graph/api/user-post-events?view=graph-rest-1.0
