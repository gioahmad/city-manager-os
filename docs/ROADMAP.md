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

## Employee / Supervisor Operations — COMPLETE

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

## GIS / Property Intelligence — COMPLETE

### #8 — Hudson County parcel + address foundation — COMPLETE

- [x] NJOGIS parcel downloader
- [x] NJOGIS NG911 address downloader
- [x] Validated Hudson County production imports
- [x] Production `gis_parcels` and `gis_addresses`
- [x] Geometry validation / repair during promotion
- [x] Spatial and lookup indexes
- [x] Address → parcel lookup
- [x] Block / lot lookup
- [x] Nearby / radius query
- [x] GIS dataset version tracking

Production counts validated September 4, 2026:

- parcels: 143,305
- addresses: 219,780
- invalid production geometries: 0

Tracked by GitHub issue #8 — completed.

### #9 — Automated GIS refresh — COMPLETE

- [x] Monthly first-Sunday refresh schedule
- [x] Download / staging / validation / atomic promotion
- [x] Prior production retained on failure
- [x] Dataset version / source-health logging
- [x] Dynamic success / failure notification
- [x] Archived raw snapshots

Tracked by GitHub issue #9 — completed.

### #10 — Flood intelligence — COMPLETE

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

Tracked by GitHub issue #10 — completed.

## Core build closeout

The original tracked core build sequence is now complete:

```text
#8 GIS FOUNDATION ✓
#9 AUTOMATED GIS REFRESH ✓
#10 FLOOD / MAP INTELLIGENCE ✓
```

City Manager OS should now be treated primarily as a production operating system with maintenance and deliberate expansion work rather than an unfinished core build.

## Optional expansion after closeout

These are enhancements, not blockers for the current City Manager OS:

- move more maintenance / configuration controls from CLI into authenticated web admin screens
- additional weather / transit / traffic / Port Authority feeds
- additional delivery channels such as SMS / email
- deeper asset hierarchy and lifecycle history
- broader department-specific workflow templates
- predictive / AI-assisted exception detection
- expanded GIS editing, layer styling, import/export and spatial analysis tools
- native mobile app if the web field experience ever proves insufficient
- public-facing views where appropriate
