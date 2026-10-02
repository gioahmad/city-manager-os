# Unified inbox and private intelligence intake

Open `/workspace#inbox` or `/workspace#library` using your existing private login. The same
responsive interface works in mobile/tablet/desktop browsers. All original navigation links
remain in **All tools**; the PC/TV page remains `/workspace/display` with work information only.

## What works in this release

* **Inbox:** Microsoft Inbox email and contact previews, personal tasks, Brain captures, files,
  work items, outsider request messages, and the most recent 30 days of area alerts in one list.
  Filter Private/Work, Needs action/All/Handled, or source. Search is over source titles and text;
  **Search everything** / Ctrl-K / Cmd-K searches all accessible records, including handled items.
  Inbox processing and local records are usable before Microsoft is connected.
* **Preview and act:** Read a source without switching pages; create a private follow-up task
  linked to that source, mark it handled, restore it to Inbox, inspect its data profile, download
  the original, or open its existing full controls. Mark handled affects only your inbox and
  does not complete, edit, or delete the source task, email, alert, or work item.
* **Context:** Link any accessible source to another record privately. Exact saved names,
  addresses, and email addresses produce evidence-labelled suggestions for People records.
  Suggestions do not create facts. Existing confirmed family inference remains separate.
  Each preview checks the current permissions of both endpoints. Links never publish private
  material into shared work or outside request portals.
* **Files:** Batch upload PDF, DOCX, XLSX, CSV, TSV, JSON, TXT, Markdown, and log files. Originals
  are retained privately. The worker extracts text, identifies email/date strings, profiles
  dataset columns/rows, and shows a sample and numeric statistics. Upload progress and queued,
  processing, ready, failed, or needs-OCR states are visible. Failed files can be retried/deleted. A short poll updates processing states; normal inbox
  refreshes run once a minute and pause while a task/link form is being edited.
* **Questions:** Ask with a specific person, street, project, or phrase to retrieve up to six
  related source excerpts using PostgreSQL text ranking. This works without an AI provider.
  An explicitly configured local Ollama model can generate a draft with numbered source
  references. Source buttons open the source preview. There are no model tools or automatic
  writes; model output never creates contacts, family relationships, messages, or work actions.
* **Microsoft 365:** Own delegated email/contact/calendar imports with automatic refresh.
  See [workspace-calendar.md](workspace-calendar.md) for Entra registration and sign-in.

## Install

Use the exact release SHA supplied with the release, from a clean repository:

```bash
bash deploy/intelligence/install_intelligence.sh EXPECTED_RELEASE_SHA
```

The installer validates a binary PostgreSQL backup with visible progress, applies migrations
036–039 idempotently, builds and tests the dashboard, checks database grants and private login,
and restarts **only the dashboard**. Live failure restores the prior dashboard image. It then
starts the separate `citymanager-intake` worker with a 768 MB memory limit, half a CPU, no
published ports, and a heartbeat healthcheck. If worker verification fails, its prior state is
restored and the verified dashboard remains installed. Database originals and additive tables
are retained. Existing staff, operations, integration, database containers, watches, recipients,
alert rules, and the SMSGate/Brain webhook are not restarted or reconfigured.

Check the worker if a file stays queued:

```bash
docker inspect citymanager-intake --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}'
docker logs --tail 30 citymanager-intake
```

Logs contain readiness errors, not private file contents, email bodies, or credentials.
An interrupted processing job is requeued after five minutes. One file is processed at a time
in a subprocess with a 55-second timeout, 45-second CPU limit, and 512 MB address-space cap.

## Optional local answers

No cloud provider is called by default. If you already run Ollama with a model installed, put
its private network endpoint and exact installed model name in the existing dashboard `.env`:

```dotenv
CMOS_OLLAMA_URL=http://citymanager-ollama:11434
CMOS_OLLAMA_MODEL=YOUR-INSTALLED-MODEL
```

For a Docker-hosted model, attach that container to the existing `citymanager` network and
use its container name `citymanager-ollama` or `ollama`. Inside the dashboard container,
`localhost` means that container, not the VPS. An RFC1918 private IP on your own network is
also accepted. Public endpoints, credential-bearing URLs, and redirects are rejected. Run
the same pinned installer after changing the environment. Model sizing depends on available
RAM/CPU/GPU; installing a model service is optional and is not performed by this release.
Only permission-filtered excerpts and the question are sent to the configured local model.

## Processing limits

This is a first working intake layer, not a replacement for a full BI warehouse or a complete
email archive. Limits are 10 files / 50 MB per upload, 20 MB per file, 500 PDF pages, 200 dataset
columns, 100,000 rows per CSV/JSON/sheet, and the first 20 workbook sheets. Search text is capped
at 200,000 characters per file; originals retain everything. Numeric statistics use the first
10,000 numeric values per column. Workbook formulas use stored cached values and are never run.
Expanded Office ZIP content is limited to 100 MB. Password-protected PDFs must be unlocked.
Image-only PDFs are labelled **Needs OCR**; no OCR or image understanding is claimed.

The list shows 60 rows at a time with Show more. Context suggestions inspect the 500 most
recent visible People/place records. Question retrieval uses terms, not embeddings, and only
the six retrieved excerpts are available to the model. Review generated drafts against those
sources. Advanced OCR, larger dataset warehousing, embeddings, automated semantic extraction,
and Superset dashboards remain further extensions rather than hidden dependencies.

If the existing HTTPS reverse proxy rejects a valid upload with HTTP 413, permit up to `55m`
request bodies for the new `/workspace/api/hub/upload` path in that existing proxy configuration.
Preserve its upstream, authentication, headers, and all other routes. The app still enforces
the tighter per-file and total limits. No proxy configuration is rewritten by the installer.

Reference: https://docs.ollama.com/api/chat
