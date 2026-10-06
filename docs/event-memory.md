# Event Memory framework — private NAS vault, references on VPS

Status: development foundation, OFF by default. Based on the Microsoft workspace release; no production service, NAS, vault, Sync subscription or storage path is assumed configured. Do not deploy or enable merely to create the framework.

## What this implements

- Event Memory in the shared navigation, plus material links on internal Events and Calendar/Event Context pages.
- Select an internal event or your own imported Microsoft appointment. Attach PDF/JPEG/PNG originals to a private vault.
- Browser computes SHA-256, obtains a signed 15-minute grant, and uploads raw bytes **directly to the NAS HTTPS bridge**. The VPS accepts metadata JSON only, bounded to 12,000 bytes. It never receives the binary and has no offline binary spool, OCR or proxy download endpoint.
- NAS streams into its own staging directory, enforces the signed size and digest, recognizes allowed file headers, fsyncs files, then atomically publishes them. A signed storage receipt is necessary before the application marks READY.
- Finalization stores small metadata and creates one private Brain link and one source relationship. Lost callbacks can be recovered with **Check NAS / recover link**. Retrying the same upload ID does not overwrite a file or multiply Brain records.
- Download uses a new signed grant, goes directly to NAS and verifies the digest in the browser. Open in Obsidian uses a vault/name URI and requires the matching vault on that device.
- File size starts at 5 MiB. Larger programs must be compressed/split before attachment. PDF/PNG/JPEG only; no HEIC, executable HTML/SVG, malware scanner or OCR in this phase. Header checks are not a security certification for untrusted PDFs.

## Privacy boundary and scope

One explicitly configured City Manager login maps to one dedicated **private** vault. This foundation does not enable shared work/official-record uploads; those need separate vaults, identity/authorization and an approved records destination. A folder inside a shared Obsidian vault is NOT a private access boundary. Do not connect this private vault to TV, staff or requester views, Obsidian Publish, or a public Git repository.

The original event may be shared; the private material list, file grants and Brain links are not. Microsoft is not modified and no invitations, texts or contact records are created. CALENDAR uses the existing local stable source UUID, not the Outlook title/date as identity. Links retain calendar sources under the existing sync pruning rules. Source deletion/unlinking and formal records retention are outside this phase.

## Filesystem layout on NAS

Use a dedicated parent directory you own, not your whole NAS share or home directory. Mount that parent at `/storage` in the bridge container:

```
/storage/
  vault/                          # Open/sync THIS directory as the private Obsidian vault
    Event Memory/<owner-hash>/EVENT-<source-uuid>/
      Event.md                    # Created once; never overwritten
      <material-uuid>/
        Material.md               # Created once; can contain your edited notes
        original.pdf              # Or original.jpg / original.png; treated as immutable
        receipt.json              # Small local manifest; no bearer tickets or credentials
  staging/                        # Not inside the synced vault
```

Material notes use full vault-relative links, JSON-quoted YAML properties and City Manager source IDs. Notes link back to the event notebook and the application. Use Obsidian backlinks to list material notes. Event titles/filenames cannot choose filesystem paths. The bridge does not overwrite existing notes or create guessed People/Work data.

## Activation prerequisites — configure, do not guess

Before activation establish: actual NAS/server address, dedicated directory, filesystem owner UID/GID, private vault name, the City Manager login authorized to use it, HTTPS dashboard origin, private HTTPS bridge origin, backup destination and the desired Obsidian sync method. NAS and all uploading devices must be able to reach the bridge. Neither the VPS nor a long-lived desktop connection proxies uploads.

Create a SEPARATE random 32-byte Event Memory key using `python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'`. Store it only in protected local configuration on the VPS and NAS. Never use the Microsoft calendar encryption key or place secrets in GitHub, a vault or screenshots.

Dashboard `.env` settings (disabled until all values are real):

```
CMOS_EVENT_MEMORY_ENABLED=false
EVENT_MEMORY_OWNER=<existing-city-manager-login>
EVENT_MEMORY_KEY=<separate-random-base64-key>
EVENT_MEMORY_BRIDGE_ORIGIN=https://<private-nas-hostname>
EVENT_MEMORY_VAULT_NAME=<actual-private-vault-name>
# Keep existing CMOS_PUBLIC_ORIGIN as your dashboard HTTPS origin.
```

NAS bridge: copy `services/event_memory_bridge/.env.example` to `.env` on the NAS, fill its real values and use matching key/owner/vault. Create the private storage parent owned by NAS_UID/NAS_GID. Build with `docker compose -f services/event_memory_bridge/compose.yml --env-file services/event_memory_bridge/.env up -d --build` **on the NAS, not the VPS**. No Docker socket or NAS administration ports should be exposed. The compose file binds to loopback by default; use a private HTTPS reverse proxy / Tailscale Serve and narrowly scoped tailnet access. Do not forward port 8099 publicly. Run one bridge worker; it limits simultaneous uploads to two and reserves 1 GiB of disk.

Use the configured HTTPS City Manager origin. Only that exact origin is permitted by NAS CORS. The dashboard CSP permits this one NAS origin only on `/event-memory`; framing/security on other modules stays unchanged. The browser must trust the NAS certificate. API tickets are sent in Authorization headers, never query strings, logs or vault metadata. Keep access logging off at the bridge and exclude Authorization headers at proxies.

The normal installer includes additive migration 043, and the feature stays disabled without settings. Activation is a separate deployment/configuration step. A NAS outage fails explicitly; retain the original scan on your device. Pending references are never falsely marked saved. A temporary upload interrupted by process/power loss may remain in NAS staging; inspect old staging manually with the bridge stopped. There is no automatic deletion of originals or orphan recovery files.

## Obsidian synchronization and backups are separate

The bridge writes ordinary Markdown and attachments; **Obsidian is not required for storage**. Obsidian Headless Sync is an optional separate NAS process, currently documented as open beta, requiring Node.js 22+ and an active Sync subscription. Do not enable headless and desktop Sync on the same vault/device or layer another bidirectional cloud sync engine over the same working folder. Keep headless credentials outside the vault, configure PDF/image attachment syncing, and confirm plan-specific file/storage limits before increasing the upload cap. No Headless/Sync deployment is installed by this framework.

A NAS storage receipt is NOT proof that Obsidian Sync finished, all devices received the file, or a backup exists. Sync encryption does not encrypt plaintext NAS/device vault copies at rest. Use device/NAS protections. Back up the vault and small VPS reference database with consistent timestamps; keep independent versioned off-site backups and verify restoration. A sync mirror/RAID is not an independent backup. If the NAS is offline, already-synced notes may be available in Obsidian but new uploads and direct browser downloads will not work.

## Tests and remaining work

Bridge tests use temporary filesystem storage; database tests use isolated `cmos_tests` PostGIS and actual application auth/CSRF/queries. No live NAS, Microsoft mailbox, real file or Sync account is used. Before live acceptance: upload a non-sensitive scan, verify its hash on NAS, confirm private event/Brain links, open it on a second synced device, disconnect NAS to verify failure/recovery, and test backup restore. These steps do not run automatically against production.

Later: camera capture UI, multi-page scan capture, off-VPS OCR/extraction with reviewed suggestions, NAS/Sync health reporting, and separately governed shared records. Existing Brain/Library uploads still use their old storage paths; they are not silently migrated. Use Event Memory for this off-VPS path.

References checked 2026-10-06:
- https://help.obsidian.md/headless
- https://help.obsidian.md/sync/headless
- https://help.obsidian.md/sync/plans
- https://help.obsidian.md/uri
