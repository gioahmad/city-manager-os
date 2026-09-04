# City Manager OS Roadmap

_Last reconciled with production/main: September 4, 2026._

## Core platform - COMPLETE

The original foundation and intelligence-loop phases are in production and accepted.

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
SOURCE -> NORMALIZE -> DATABASE -> WATCHLIST / RULES -> RECIPIENTS -> DELIVERY -> AUDIT
```

## Executive / Command interface - COMPLETE

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

## Employee / Supervisor Operations - COMPLETE

- [x] Public HTTPS Employee Operations portal
- [x] Employee ID + PIN authentication
- [x] Managed employees / departments / locations / work types
- [x] Report Issue / My Work
- [x] Automatic priorities and checklists
- [x] Before / after photos
- [x] Inline photo viewer with zoom and explicit download option
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

## GIS / Property Intelligence - COMPLETE

### #8 - Hudson County parcel + address foundation - COMPLETE

- [x] NJOGIS parcel downloader
- [x] NJOGIS NG911 address downloader
- [x] Validated Hudson County production imports
- [x] Production `gis_parcels` and `gis_addresses`
- [x] Geometry validation / repair during promotion
- [x] Spatial and lookup indexes
- [x] Address -> parcel lookup
- [x] Block / lot lookup
- [x] Nearby / radius query
- [x] GIS dataset version tracking

Production counts validated September 4, 2026:

- parcels: 143,305
- addresses: 219,780
- invalid production geometries: 0

Tracked by GitHub issue #8 - completed.

### #9 - Automated GIS refresh - COMPLETE

- [x] Monthly first-Sunday refresh schedule
- [x] Download / staging / validation / atomic promotion
- [x] Prior production retained on failure
- [x] Dataset version / source-health logging
- [x] Dynamic success / failure notification
- [x] Archived raw snapshots

Tracked by GitHub issue #9 - completed.

### #10 - Flood intelligence - COMPLETE

- [x] FEMA NFHL effective flood-zone acquisition / import
- [x] Local PostGIS flood-zone storage
- [x] Interactive Flood Intelligence map
- [x] General Mapping Center with system and custom layers
- [x] Parcels / NG911 addresses / watch locations / operational issues map layers
- [x] Custom GeoJSON layers and browser-drawn points / lines / polygons
- [x] NOAA CO-OPS The Battery station 8518750 live water-level observations
- [x] NOAA flood thresholds loaded dynamically from authoritative station metadata
- [x] NWS active flood / coastal alerts for the Weehawken point
- [x] Live observation storage in `flood_observations`
- [x] `NOAA_TIDE` and `NWS_FLOOD` source-health monitoring
- [x] NOAA threshold-change alerts only, avoiding routine-observation notification noise
- [x] Official NWS flood alerts normalized into the standard alert schema
- [x] Existing central watchlist matcher and dynamic subscriber routing retained
- [x] FEMA-zone watched-location context included in flood alerts
- [x] Live 5-minute n8n production trigger validated successfully

Static flood production validation:

- FEMA flood polygons: 1,081
- SFHA polygons: 149
- invalid geometries: 0

Tracked by GitHub issue #10 - completed.

## Core build closeout

The original tracked core build sequence is complete:

```text
#8 GIS FOUNDATION ✓
#9 AUTOMATED GIS REFRESH ✓
#10 FLOOD / MAP INTELLIGENCE ✓
```

City Manager OS should now be treated primarily as a production operating system with maintenance and deliberate expansion work rather than an unfinished core build.

# Active Next Phase

The next phase prioritizes ease of administration and operational value over additional raw feature volume.

## Priority 1 - Web Administration v2

### Watchlists / Subscribers / Routing
- Improve the existing web UI rather than replacing the working routing architecture.
- Make create/edit/activate/deactivate flows simpler and more obvious.
- Make recipient assignment and routing relationships easier to understand at a glance.
- Add concise inline instructions and examples.
- Preserve all PostgreSQL-backed dynamic routing and existing matcher behavior.

### Integrations Center
Create one authenticated web location for managing external APIs and data sources.

The center should eventually support:
- integration name and type
- source identifier
- base URL / endpoint metadata
- authentication method metadata without exposing secrets
- enable / disable
- test connection
- last successful run
- last failure / error summary
- polling cadence or webhook mode
- normalization target
- source-health status
- links to related watchlists / rules
- notes and ownership

Secrets remain in protected server configuration or secret storage, never committed to GitHub or casually displayed in the browser.

## Priority 2 - Events Center

Add a first-class Events area for municipal events, meetings, deadlines, hearings, ribbon cuttings, construction milestones and other scheduled activity.

Events should support:
- title
- date / time
- location
- owner
- status
- people / agencies involved
- preparation checklist
- notes
- related documents / links
- related existing Command Center issues
- reminders / follow-up

An event should not automatically become an issue. Action items created from an event must use the existing Command Center / `issues` source of truth.

## Priority 3 - Transit / External Intelligence Expansion

### NJ Transit
Add NJ Transit feeds using the Integrations Center pattern.

Operational goal:
- surface service disruptions that matter to Weehawken
- normalize actionable disruptions into the existing Standard Alert Schema where appropriate
- use current watchlist / subscriber routing
- summarize routine service information rather than generating alert noise
- support location / route / station / service-area context where available

### Additional sources after NJ Transit
Potential future sources include NJ 511, PATH, Port Authority, traffic, road closures and other authoritative regional feeds. Add only when operational value is clear.

## Priority 4 - Executive Operating Layer

Improve the existing executive features rather than creating a parallel task system.

Focus areas:
- Daily Manager Brief
- "What changed" since prior review
- Waiting On / commitments
- approaching deadlines
- exception visibility
- meaningful escalation
- meeting preparation context

## Priority 5 - In-App Instructions

Add short, practical help directly to administrative pages.

Preferred format:
- What this page does
- How to use it
- 1-3 concrete examples

Avoid long manuals inside the working UI.

## Deferred - Obsidian / Local Documents

Obsidian or local-document integration is intentionally deferred until the web administration, Events Center, Integrations Center and NJ Transit work are stable.

When revisited, Obsidian should function as a knowledge/document source, not as a replacement for Command Center operational records.

## Architecture rules for all next-phase work

- inspect production and repository state before changes
- do not rebuild accepted features
- smallest sensible change
- preserve operational data
- `issues` remains the only operational issue/task source of truth
- dynamic PostgreSQL subscriber routing remains authoritative
- n8n remains the automation engine, not the main UI
- move frequently changed operational configuration into authenticated web UI
- no fake recurring municipal work
- do not casually alter frozen navigation / palette
- back up before significant changes
- health and functional verification after changes
- clean synthetic test data
- successful production changes are committed and pushed to `main`
- long-running scripts must clearly end with `COMPLETE: PASS` or `COMPLETE: FAIL`
