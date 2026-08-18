# Dashboard

## Purpose

The dashboard is the main operating interface for City Manager OS. Normal monitoring should happen here, not in the n8n editor.

## Initial v0.1 Home Screen

```text
CITY MANAGER OS

LIVE MAP            ACTIVE ALERTS
UTILITIES           WEATHER / FLOOD
TRAFFIC             TRANSIT
WATCHLIST           EVENTS
INTELLIGENCE FEED

+ NEW ISSUE
```

## Initial Views

### Live
- Current critical/important/advisory items
- Utilities
- Weather/flood
- Public safety
- Traffic/transit
- Events
- Watchlist matches

### Map
Toggle layers such as:
- Active incidents
- PSEG outages
- Fire
- Traffic
- Weather/flood
- Watchlist
- Critical facilities
- Parcels
- Flood zones

### Watchlist
- Search and filter watch items
- Add/edit watch items
- Recipient assignments
- GIS metadata
- Notes

### Intake
Fast manual entry for issues received by phone, text, staff, elected officials, residents, contractors, or observation.

Suggested fields:
- What happened
- Location
- Category
- Priority
- Source
- Assignment
- Notes
- Add location/item to watchlist

### Intelligence Feed
One chronological feed containing normalized events from all sources, filterable by category/source/priority/watchlist status.

### Source Health
Show last successful update/heartbeat for each collector and stale/failed source warnings.

## Principle

The dashboard shows the operating picture.

ntfy interrupts when something requires attention.

n8n runs behind the scenes.
