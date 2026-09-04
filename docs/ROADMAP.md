# City Manager OS Roadmap

_Last reconciled with production/main: September 4, 2026._

## Core platform — COMPLETE

The original foundation and intelligence-loop phases are in production.

- [x] PostgreSQL + PostGIS shared data model
- [x] Database-backed Master Watchlist
- [x] Standard Alert Schema
- [x] Subscriber Directory
- [x] Central Watchlist Matcher
- [x] Dynamic recipient resolution
- [x] Delivery deduplication / guard
- [x] Delivery logging and audit history
- [x] Source health tracking
- [x] ntfy routing
- [x] PSEG and additional normalized operational sources
- [x] Health / deployment tooling

Core design remains:

```text
SOURCE → NORMALIZE → DATABASE → WATCHLIST / RULES → RECIPIENTS → DELIVERY → AUDIT
```

## Executive / Command interface — COMPLETE

Normal municipal monitoring and follow-up no longer require opening n8n.

- [x] Live dashboard / overview
- [x] Alerts and intelligence feed
- [x] Command Center / issue tracking
- [x] My Day executive view
- [x] Schedule / meeting prep
- [x] Decision Desk
- [x] Visibility Queue
- [x] Rules Center
- [x] Watchlist / subscribers / routing management
- [x] Source Health / Deliveries
- [x] Executive Assistant / morning brief / proactive follow-up
- [x] Unified responsive desktop + mobile interface

The basic live map was intentionally moved out of the original Dashboard v0.1 milestone and now belongs to the GIS work below.

## Employee / Supervisor Operations — COMPLETE

- [x] Public HTTPS Employee Operations portal
- [x] Employee ID + PIN authentication
- [x] Managed employees / departments / locations / work types
- [x] Report Issue / My Work
- [x] Automatic priorities and checklists
- [x] Before / after photos
- [x] Start Work
- [x] Need Help / Can't Complete
- [x] Completion workflow
- [x] Supervisor assignment / reassignment / instructions
- [x] Operations Board
- [x] Supervisor completion verification / return to work
- [x] Recurring work engine
- [x] Expected daily activity / awareness timeline
- [x] Missed-activity exceptions
- [x] Routine start / end dates
- [x] Per-occurrence Today Notes
- [x] Eastern Time application/session standardization

## Current finish line — GIS / Property Intelligence

### #8 — Hudson County parcel + address foundation — ACTIVE / NEXT

Already built in GitHub:

- [x] County-selectable NJOGIS parcel downloader
- [x] County-selectable NJOGIS NG911 address downloader
- [x] PostGIS staging importer with row-count / SRID validation

Still required:

- [ ] Verify current Hudson County parcel snapshot on VPS
- [ ] Verify current Hudson County address snapshot on VPS
- [ ] Import both datasets into staging
- [ ] Inspect fields / geometry / row counts
- [ ] Promote validated production `gis_parcels` and `gis_addresses`
- [ ] Add production spatial and lookup indexes
- [ ] Address → parcel lookup
- [ ] Block / lot lookup
- [ ] Nearby / radius query
- [ ] Connect geographic context to dashboard/watchlist where useful

Tracked by GitHub issue #8.

### #9 — Automated GIS refresh — BLOCKED BY #8

After the first production import path is proven:

- [ ] Scheduled dataset download
- [ ] Staging import
- [ ] Validation
- [ ] Promote only on success
- [ ] Retain prior production data on failure
- [ ] Record dataset version / refresh status
- [ ] Success / failure notification

Tracked by GitHub issue #9.

### #10 — Flood intelligence — AFTER GIS FOUNDATION

- [ ] Static flood-zone acquisition / import
- [ ] Live rain / tide / flood observation sources
- [ ] Normalized flood events / alerts
- [ ] Flood map layer
- [ ] Spatial watch rules
- [ ] Critical notification thresholds
- [ ] Test watched facilities / properties against flood conditions

Tracked by GitHub issue #10.

## Optional expansion after closeout

These are enhancements, not blockers for the current City Manager OS:

- additional weather / transit / traffic / Port Authority feeds
- additional delivery channels such as SMS / email
- deeper asset hierarchy and lifecycle history
- broader department-specific workflow templates
- predictive / AI-assisted exception detection
- native mobile app if the web field experience ever proves insufficient
- public-facing views where appropriate

## Project closeout condition

The core City Manager OS is operational now. The remaining tracked build sequence is:

```text
#8 FIRST GIS IMPORT → #9 AUTOMATED GIS REFRESH → #10 FLOOD / MAP INTELLIGENCE
```

When #8, #9 and #10 are complete, there should be no remaining open build issues required for the original core system vision.
