# Roadmap

## Phase 1 — Foundation

**Goal:** establish the shared data model and project backbone before adding more feeds.

- [ ] Finalize Master Watchlist schema
- [ ] Finalize Standard Alert Schema
- [ ] Define PostgreSQL/PostGIS database model
- [ ] Deploy PostgreSQL + PostGIS
- [ ] Connect n8n to database
- [ ] Create Subscriber Directory
- [ ] Create Delivery Log

**Exit condition:** one stable database-backed watchlist and subscriber model exists.

## Phase 2 — Working Intelligence Loop

**Goal:** prove one complete source-to-alert loop.

```text
SOURCE → n8n → DATABASE → WATCHLIST MATCH → RECIPIENTS → ntfy → LOG
```

- [ ] Build Central Watchlist Matcher
- [ ] Build recipient resolver
- [ ] Add duplicate suppression
- [ ] Add delivery logging
- [ ] Connect PSEG as first live source
- [ ] Validate PSEG → Watchlist → ntfy

**Exit condition:** PSEG reliably enters the OS, matches the watchlist, notifies the right users, and logs delivery.

## Phase 3 — Dashboard v0.1

- [ ] Live alerts view
- [ ] Intelligence feed
- [ ] Watchlist editor
- [ ] New Issue intake
- [ ] Source health
- [ ] Utility status
- [ ] Basic live map

**Exit condition:** normal monitoring no longer requires opening n8n.

## Phase 4 — GIS / Property Intelligence

- [ ] Import Hudson County parcels
- [ ] Import address points
- [ ] Resolve block/lot/parcel IDs
- [ ] Add coordinates to watchlist
- [ ] Add nearby property search
- [ ] Add radius-based watch matching
- [ ] Add flood zones
- [ ] Establish monthly GIS refresh workflow

## Phase 5 — Operational Intelligence Expansion

Add and normalize additional sources:

- Fire SDR
- Weather / NWS
- Flood gauges / tides
- Traffic / NJ 511
- Transit / PATH / NJ Transit
- Port Authority
- Events
- Road closures
- Construction
- Air quality
- Internal municipal issues

## Parking Lot

Good ideas that should not distract from the current phase:

- SMS
- Email alerts
- Telegram
- Acknowledgment workflows
- Escalations
- AI summaries
- Daily briefing
- Native mobile app
- Predictive alerts
- Staff-specific dashboards
- Public-facing dashboard
