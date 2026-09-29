# Brain

Brain is a private `/brain` page in the existing dashboard. Open the same HTTPS
City Manager OS address on iPhone, iPad, Android, Windows or Mac and sign in with
the same account. Safari's **Share → Add to Home Screen** offers a shortcut on
iOS/iPadOS; Android browsers offer **Add to Home screen**. This is an online web
page, with no offline cache of private notes.

## Included

Capture text and links, one attachment up to 20 MB per note, edit, pin, search,
filter, trash/restore, and export note text/tags as JSON. Original attachments
download individually. Notes belong to the signed-in username; other accounts
cannot read or edit them. Existing private authentication must be enabled.
Read-only accounts can read/export their own notes but cannot write.

Search uses PostgreSQL English full-text stemming plus literal phrase/tag
matching. It does not provide AI/semantic answers, OCR, automatic people/project
extraction or reminders. Basic suggested types recognize a leading idea/task
phrase or a URL; hashtags become tags. Nothing creates alerts, watches, contacts
or operational tasks automatically.

The separate module shares the dashboard process, login and Postgres. It adds
three tables and no containers or background workers. HTTPX is added for API tests.
Attachments are stored transactionally in Postgres and included in existing
database backups. The 20 MB limit is per attachment; normal database/backup disk
usage grows as attachments accumulate. Trashing keeps notes and attachments for
restoration. No permanent delete is implemented.

## Existing SMSGate

The existing SMSGate send configuration is unchanged. Brain receives signed
`sms:received` events from the same Android gateway; no Twilio account is needed.

1. Sign in as Executive, open Brain → **Access from your devices & SMS**.
2. Enter allowed originating phone numbers and the Android device's **Settings →
   Webhooks → Signing Key**. Use country codes; ten-digit US numbers receive +1.
   Messages from these numbers save into the account configuring this connection.
   One gateway owner is supported initially.
3. Click **Connect existing SMSGate** to register the destination using the
   credentials already saved on the Share page. `CMOS_PUBLIC_ORIGIN` must be your
   HTTPS City Manager OS origin. The button checks for an existing Brain destination
   and does not remove other webhooks. For a custom/n8n gateway, manually add an
   `sms:received` webhook destination:
   `https://YOUR-CMOS-HOST/brain/webhooks/sms`. Keep other destinations intact.
   SMSGate's API offers `POST /3rdparty/v1/webhooks` for Cloud/Private modes,
   with `url`, `event: sms:received`, and optionally `device_id`.
4. If a reverse proxy/access gateway normally challenges every request for login,
   allow only POST to this exact webhook path through. Brain verifies HMAC itself.
   If your existing n8n SMS webhook already receives events, forward the untouched
   raw body and `X-Timestamp`/`X-Signature` headers to this path. JSON reserialization
   changes the signature. Never disable authentication for all of `/brain`.
5. Text the SIM number used by your Android gateway from an allowed phone. Check
   the Brain feed for source **SMS**. Repeat the delivery to confirm one note.

Signing follows SMSGate's raw-body-plus-timestamp HMAC-SHA256 contract. Requests
older than five minutes are rejected; original event IDs prevent duplicates.
Gateway clock alignment and freshly signed retry delivery are required. Current
SMSGate `sender` and older `phoneNumber` payload fields are supported. Unsupported
events are ignored. Incoming MMS/photos and automatic SMS replies are not enabled.
Registration and a real incoming text must be verified on the user's VPS/device;
repository tests cannot verify the user's carrier or reverse proxy.

Official protocol reference: https://docs.sms-gate.app/features/webhooks/

## Deployment

After the PR is merged and the production checkout updated to the reviewed commit:

```bash
cd /opt/city-manager-os
bash deploy/brain/install_brain.sh "$(git rev-parse HEAD)"
```

The installer requires a clean checkout and an expected commit, validates an
existing backup, applies the additive migration, builds/tests the image and
checks the database/login before replacing only the dashboard. If dashboard
startup verification fails it restores the previous image. Staff, ops-engine,
integration-engine, n8n, and the spatial/watch matcher keep running.

Rollback after successful installation: redeploy the previous dashboard image or
commit using the existing deployment process. Keep the additive tables and their
data; no destructive reverse migration is required.
