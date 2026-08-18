# City Manager OS — Project Blueprint

## Purpose

This is the single source of truth for what City Manager OS is intended to become, why the architecture is structured this way, what already exists, and what comes next.

## System Vision

City Manager OS combines live external intelligence, internal issue intake, GIS/property intelligence, master watchlists, alerts and notifications, operational dashboards, tasks/follow-up, and automation.

Core concept:

```text
SEE IT → TRACK IT → TELL ME
```

## Core Architecture

```text
DATA SOURCES
     ↓
    n8n
collection / parsing / normalization
     ↓
PostgreSQL + PostGIS
     ↓
City Manager OS Dashboard
     ↓
Watchlist / Rules / Intelligence
     ↓
n8n Trigger Engine
     ↓
ntfy / future SMS / Email
```

## Major Modules

| Module | Status | Purpose |
|---|---|---|
| n8n Automation Engine | Existing | Collect and process data |
| ntfy Notifications | Existing | Push alerts |
| Standard Alert Schema | Designed | Common format for every alert |
| Central Alert Router | Designed | Watchlist-first routing |
| Master Watchlist | Designing | Central list of important things |
| Subscriber Directory | Designing | Users and delivery destinations |
| GIS / Property Layer | Designing | Parcels, block/lot, proximity |
| PostgreSQL/PostGIS | Not built | Central data store |
| Live Dashboard | Not built | Main operating picture |
| Intake System | Not built | Manual issues/requests |
| Intelligence Feed | Not built | Unified event timeline |
| Flood / Weather Layers | Not built | Live and GIS flood intelligence |
| Utility Monitoring | Partial | PSEG automation exists |
| Fire Intelligence | Partial | SDR / transcription work |
| Traffic / Transit | Planned | Regional disruptions |
| Event Intelligence | Research/design | Regional event collection |
| Task / Follow-up | Planned | Operational issue management |

## Watchlist Design

The Master Watchlist is the central intelligence list. Example watch items include addresses, facilities, areas, phrases, sources, and incident types.

Each watch item can carry: watch ID, active status, type, search term, display name, aliases, match mode, category/subcategory, address, municipality/county/state/ZIP, block/lot/qualifier, parcel ID, latitude/longitude, radius, GIS/nearby flags, notes, and recipient assignments.

## GIS Strategy

Do not depend on live ArcGIS calls during incidents.

```text
NJ GIS / County GIS
      ↓
Scheduled download
      ↓
Local PostGIS
      ↓
n8n / Dashboard
```

Potential local datasets include parcels, block/lot, address points, flood zones, municipal buildings, schools, senior facilities, hospitals, roads, building footprints, critical infrastructure, and watch areas.

## Notification Architecture

Rejected model:

```text
PSEG → separate subscriber list
Fire → separate subscriber list
Weather → separate subscriber list
```

Preferred model:

```text
Alert
 ↓
Master Watchlist
 ↓
Find matching watch items
 ↓
Collect recipients from matched rows
 ↓
Remove duplicate recipients
 ↓
Subscriber Directory
 ↓
ntfy
 ↓
Delivery Log
```

## Dashboard Role

The dashboard is the operating interface. n8n remains the automation engine behind it.

Initial dashboard concept:

```text
CITY MANAGER OS

LIVE MAP            ACTIVE ALERTS
UTILITIES           WEATHER / FLOOD
TRAFFIC             TRANSIT
WATCHLIST           EVENTS
INTELLIGENCE FEED

+ NEW ISSUE
```

## Existing / Partial Data Sources

### PSEG
- Customer outages
- Municipal outage status
- ETR
- Outage start
- Jobs/circuits

### Fire SDR
- SDR / Raspberry Pi
- Audio capture
- Speech-to-text
- Incident parsing

## Planned Sources

ORU, NWS, flood gauges, tide information, NJ 511, NJ Transit, PATH, Port Authority, events/venues, air quality, road closures, construction, and internal municipal issues.

## Security Boundary

GitHub stores architecture, sanitized examples, schemas, documentation, and workflow exports. Production operational data belongs on the controlled server/database and should not be committed to this repository.
