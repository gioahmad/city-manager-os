# City Manager OS

**Municipal Operations, Intelligence & Automation Platform**

City Manager OS is a private municipal operations platform for combining live intelligence, internal issues, GIS/property context, watchlists, automation, and targeted notifications into one operating picture.

## Operating Model

> **SEE IT → TRACK IT → TELL ME**

- **See It** — dashboard, live map, intelligence feed, source health
- **Track It** — watchlist, incidents, intake, GIS context, tasks/follow-up
- **Tell Me** — n8n rules, watchlist matching, ntfy notifications, future escalation channels

## Current Phase

**Phase 1 — Foundation / Architecture**

Existing:
- n8n automation engine
- ntfy notifications
- PSEG outage work
- Fire/SDR intelligence work

Next foundation:
- Master Watchlist schema
- Standard Alert Schema
- PostgreSQL + PostGIS
- Central watchlist-first router
- Delivery logging

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

## Security Rule

This repository is for architecture, schemas, sanitized examples, documentation, and exported workflows. Do **not** commit passwords, API keys, production credentials, confidential resident information, or live sensitive municipal intelligence.
