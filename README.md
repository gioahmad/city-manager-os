# City Manager OS

**Municipal Operations, Intelligence & Automation Platform for Weehawken**

City Manager OS is a production municipal operating system that combines live intelligence, operational issues, employee and supervisor field work, GIS/property context, watchlists, automation, recurring operations, executive follow-up, and targeted notifications into one operating picture.

## Operating Model

> **SEE IT → TRACK IT → TELL ME**

- **See It** - live intelligence, maps, source health, field reports, schedules and events
- **Track It** - Command Center, recurring work, follow-up, commitments and related municipal context
- **Tell Me** - rules, watchlist matching, dynamic subscriber routing, ntfy and executive summaries

## Production Status

The original core build is accepted and in production.

- PostgreSQL + PostGIS operational foundation
- Command Center using the existing `issues` table as the source of truth
- Employee Operations and Supervisor Operations Board
- recurring Operations Engine and awareness routines
- Master Watchlist, subscriber directory and dynamic routing
- delivery guard, deduplication and audit history
- GIS parcels, NG911 addresses and Mapping Center
- FEMA flood intelligence
- NOAA/NWS live flood monitoring
- Executive Assistant / proactive workflows
- monthly GIS refresh

The system should now be treated as a production operating system with deliberate expansion work, not as an unfinished core build.

## Active Next Phase

1. **Watchlist / Subscriber Admin v2** - make existing watchlists, recipients and routing materially easier to manage from the web.
2. **Events Center** - track municipal events, meetings, deadlines and operational preparation without turning every event into an issue.
3. **Integrations Center** - web-based administration for APIs and external data sources, including connection testing, enable/disable controls, source health and configuration metadata.
4. **NJ Transit Integration** - add useful NJ Transit operational feeds through the new integrations pattern and existing alert/watchlist pipeline.
5. **Simple In-App Help** - short instructions and examples on administrative pages so normal use does not require CLI knowledge or a large manual.
6. **Executive Operating Layer** - improve daily brief, waiting-on/commitments, exception visibility and "what changed" awareness.
7. **Obsidian / Local Documents** - intentionally deferred until the operational and integration work above is complete.

## Non-Negotiable Architecture Rules

- Do not rebuild accepted components without a verified defect or explicit request.
- The existing `issues` table / Command Center remains the operational source of truth. Do not create a parallel issue/task database.
- Notification destinations remain dynamic in PostgreSQL. Do not hard-code recipients into source workflows.
- n8n is the automation engine, not the primary user interface.
- Frequently changed operational configuration should move toward authenticated web administration.
- Preserve production data and back up before significant changes.
- Keep the existing desktop navigation palette and frozen mobile navigation unless a deliberate navigation change is approved.
- Do not commit API keys, passwords, production credentials, confidential resident data or live sensitive municipal intelligence to GitHub.

## Project Documents

- [Project Blueprint](docs/PROJECT_BLUEPRINT.md)
- [Roadmap](docs/ROADMAP.md)
- [Feature Matrix](docs/FEATURE_MATRIX.md)
- [Decision Log](docs/DECISION_LOG.md)

## Modules

- [Master Watchlist](modules/WATCHLIST.md)
- [GIS / Property Intelligence](modules/GIS.md)
- [Alert Router](modules/ALERT_ROUTER.md)
- [Dashboard](modules/DASHBOARD.md)
- [Data Sources](modules/DATA_SOURCES.md)

## Schemas / Templates

- [Master Watchlist CSV](schemas/MASTER_WATCHLIST.csv)
- [Standard Alert Schema](schemas/ALERT_SCHEMA.json)
- [Database Model](schemas/DATABASE_SCHEMA.md)
