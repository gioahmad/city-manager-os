# City Manager OS - Project Blueprint

## Purpose

This is the single source of truth for what City Manager OS is intended to become, why the architecture is structured this way, what already exists, and what comes next.

## System Vision

City Manager OS is the municipal operating system and executive second brain for Weehawken.

Core concept:

```text
SEE IT -> TRACK IT -> TELL ME
```

Operational interpretation:

```text
SIGNAL -> CONTEXT -> ACTION -> ACCOUNTABILITY -> MEMORY
```

The system should combine live intelligence, internal issues, employee and supervisor field operations, recurring municipal work, GIS/property context, events, external APIs, watchlists, dynamic notification routing, executive follow-up and institutional memory.

## Production Core

The original core is accepted and in production.

- PostgreSQL + PostGIS
- Command Center / existing `issues` table
- Employee Operations
- Supervisor Operations Board
- recurring Operations Engine
- Master Watchlist
- Subscriber Directory
- dynamic recipient routing
- Delivery Guard / deduplication / audit
- GIS parcels and NG911 addresses
- Mapping Center
- FEMA flood intelligence
- NOAA/NWS live flood monitoring
- Executive Assistant / proactive workflows
- monthly GIS refresh

Do not rebuild accepted components unless explicitly requested or a verified defect requires it.

## Core Architecture

```text
EXTERNAL / INTERNAL SOURCES
          ↓
         n8n
collection / parsing / normalization
          ↓
PostgreSQL + PostGIS
          ↓
City Manager OS Web Interface
          ↓
Watchlists / Rules / Events / Operations / Command Center
          ↓
Dynamic Recipient Routing
          ↓
ntfy / future approved delivery channels
          ↓
Audit / Institutional Memory
```

## Operational Data Rule

The existing `issues` table / Command Center remains the single operational issue/task source of truth.

Do not create a parallel issue or task database for Events, integrations, commitments, field work or executive follow-up.

Other records may represent awareness, events, commitments, source state or municipal entities, but when accountable action is required it should link to or create a normal Command Center issue.

## Administration Rule

n8n remains the automation engine, not the normal administration interface.

Frequently changed municipal configuration should move toward authenticated web administration.

This includes:
- watchlists
- subscribers
- routing
- routines
- events
- external integrations
- source health
- alert thresholds / rule metadata where safe

## Next-Phase Modules

### Watchlist / Subscriber Admin v2
Improve the existing web management experience without replacing the working database-backed matcher or recipient resolver.

### Events Center
Track municipal events, meetings, deadlines and preparation. Events are not issues by default; actionable follow-up uses Command Center.

### Integrations Center
Provide one web location to manage external APIs and data sources, see health, test connectivity, enable/disable integrations and understand where each source feeds the system.

Secrets remain protected server-side and are never committed to GitHub.

### NJ Transit
Add useful NJ Transit operational intelligence through the Integrations Center pattern and existing Standard Alert Schema / Watchlist / Subscriber pipeline.

### Executive Operating Layer
Improve Daily Manager Brief, Waiting On / commitments, deadlines, exceptions and changed-since-last-review awareness.

### In-App Help
Short contextual instructions and examples inside administrative pages rather than large manuals.

### Obsidian / Local Documents
Deferred until the active operational and integrations work is stable. Obsidian should eventually serve as a knowledge/document source, not replace operational records.

## GIS Strategy

Static or slow-changing GIS should be stored locally in PostGIS rather than depended upon live during incidents.

```text
AUTHORITATIVE GIS SOURCE
        ↓
validated scheduled acquisition
        ↓
local PostGIS
        ↓
Mapping / enrichment / watch context
```

Location-aware external feeds should reuse the existing GIS layer when route, station, stop, facility, parcel, area or proximity context is available.

## Notification Architecture

Rejected model:

```text
PSEG -> separate subscribers
NJ Transit -> separate subscribers
Weather -> separate subscribers
```

Accepted model:

```text
Normalized signal
      ↓
Master Watchlist / Rules
      ↓
Matched watch items
      ↓
Dynamic recipient resolution
      ↓
Delivery Guard
      ↓
ntfy
      ↓
Audit
```

No source-specific hard-coded recipients.

## Awareness vs Action

Not every signal should become an issue.

- **Awareness** - useful information, no action required
- **Watch** - condition may matter if it changes
- **Action** - accountable work is required and belongs in Command Center

A good test for creating an issue is:

> An action is required + someone can own it + completion can be verified + delay matters.

## Role Views

### Township Manager
Needs exceptions, decisions, commitments, deadlines, changes and cross-department visibility.

### Supervisor
Needs team workload, unassigned work, overdue work, verification and operational exceptions.

### Field Employee
Needs only assigned work, location, instructions, checklist, photos and completion controls.

## Security Boundary

GitHub stores architecture, sanitized examples, schemas, documentation and exported workflows.

Do not commit passwords, API keys, production credentials, confidential resident information or live sensitive municipal intelligence.
