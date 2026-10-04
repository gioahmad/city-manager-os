# Feature Matrix

_Last reconciled with the preserve-first production release: October 4, 2026._

| Feature | Status | Priority | Lives In | Next Action |
|---|---|---:|---|---|
| n8n Automation Engine | Working | Critical | n8n | Keep as backend engine |
| PostgreSQL / PostGIS | Working | Critical | Server / PostGIS | Maintain / back up |
| Master Watchlist | Working | Critical | Postgres / Dashboard | Improve web management UX, preserve matcher |
| Subscriber Directory | Working | High | Postgres / Dashboard | Improve web management UX |
| Routing Management | Working | High | Postgres / Dashboard | Make assignments easier to understand and edit |
| Standard Alert Schema | Working | High | schemas / n8n | Maintain compatibility |
| Central Watchlist Matcher | Working | Critical | n8n / Postgres | Maintain |
| Recipient Resolver | Working | High | n8n / Postgres | Maintain |
| Delivery Guard / Deduplication | Working | High | n8n / Postgres | Maintain |
| Delivery Logging / Audit | Working | High | Postgres / Dashboard | Maintain |
| ntfy Notifications | Working | Critical | ntfy / n8n | Maintain dynamic routing |
| Source Health | Working | High | Postgres / Dashboard | Extend to all new integrations |
| PSEG Outages | Working | High | n8n / Postgres / Dashboard | Maintain |
| Multi-source Operational Alerts | Working | High | n8n / Postgres | Expand only when useful |
| Live Dashboard / Overview | Working | High | FastAPI / Postgres | Maintain |
| Alerts / Intelligence Feed | Working | High | Dashboard / Postgres | Maintain |
| Command Center / Issue Tracking | Working | Critical | Dashboard / Postgres | Remains operational source of truth |
| My Day | Working | Critical | Dashboard / Postgres | Improve exception / changed-since-last-review focus |
| Schedule / Meeting Prep | Working | High | Dashboard / Postgres | Connect to Events / commitments where useful |
| Decision Desk | Working | High | Dashboard / Postgres | Maintain |
| Visibility Queue | Working | Medium | Dashboard / Postgres | Maintain |
| Rules Center | Working | High | Dashboard / Postgres | Extend only as needed |
| Executive Assistant / Morning Brief | Working | High | n8n / Dashboard / Postgres | Improve Daily Manager Brief and Waiting On |
| Executive Intake | Working | High | Dashboard / Microsoft Graph / Postgres | Keep Microsoft read-only; user decides what becomes Work / Brain / Event |
| Universal Context Inspector | Working | High | Dashboard / existing context links | Expand evidence and record-specific actions without parallel graph tables |
| Macro-inspired Shell / Quick Look | Working | High | Dashboard | Preserve all module power while improving search, sidecar and keyboard navigation |
| Alert Timeline Repair | Working | High | Dashboard / Postgres | Maintain audited Activity Time corrections and batch repair workflows |
| Employee Operations Portal | Working | Critical | FastAPI / Postgres | Maintain |
| Supervisor Operations Board | Working | Critical | Dashboard / Postgres | Maintain |
| Employee Photos / Checklists | Working | High | FastAPI / storage / Postgres | Maintain |
| Supervisor Verification | Working | High | Dashboard / Postgres | Maintain |
| Recurring Work Engine | Working | High | Operations Engine / Postgres | Configure real routines only |
| Daily Awareness / Expected Activity | Working | High | Operations Engine / Dashboard | Configure real routines only |
| Routine Start / End Dates | Working | Medium | Operations Engine / Postgres | Maintain |
| Occurrence-specific Today Notes | Working | Medium | Operations Engine / Dashboard | Maintain |
| GIS Parcel Downloader | Working | High | deploy/gis | Monthly refresh maintains data |
| GIS Address Downloader | Working | High | deploy/gis | Monthly refresh maintains data |
| GIS Staging Import | Working | High | deploy/gis / PostGIS | Maintain validation gates |
| Production GIS Parcels | Working | High | PostGIS | Maintain |
| Production GIS Addresses | Working | High | PostGIS | Maintain |
| Block / Lot Enrichment | Working | Medium | PostGIS | Extend as needed |
| Nearby / Radius Search | Working | Medium | PostGIS | Extend as needed |
| Automated GIS Refresh | Working | Medium | systemd / PostGIS / routing | Monitor monthly run |
| Mapping Center | Working | High | Dashboard / PostGIS | Use for location-aware external intelligence |
| System Map Layers | Working | High | Dashboard / PostGIS | Maintain flood / parcel / address / watch / issue layers |
| Custom GIS Layers | Working | Medium | Dashboard / PostGIS | Add styling / editing tools only when useful |
| GeoJSON Import / Web Drawing | Working | Medium | Dashboard / PostGIS | Expand later if needed |
| FEMA Flood Zones | Working | High | PostGIS / Dashboard | Maintain |
| Flood Intelligence Dashboard | Working | High | Dashboard / PostGIS | Maintain |
| NOAA Tide / Water Level Monitor | Working | High | n8n / Postgres | Maintain |
| NWS Flood / Coastal Alerts | Working | High | n8n / Postgres | Maintain |
| Flood Spatial Watch Context | Working | High | PostGIS / n8n | Add real watched facilities as configured |
| Flood Dynamic Alert Routing | Working | Critical | n8n / Postgres / ntfy | Maintain central matcher path |
| Watchlist / Subscriber Admin v2 | Working | Critical | Dashboard / Postgres | Continue UX polish; preserve matcher and routing model |
| Events Center | Working | High | Dashboard / Postgres | Continue contextual linking and preparation polish |
| Integrations Center | Working | Critical | Dashboard / n8n / Postgres | Continue source onboarding / operational health polish |
| NJ Transit / Transit Intelligence | Working | High | Dashboard / integrations / Postgres | Maintain relevance scoring and expand only with operational value |
| Location-aware Integration Context | Working | High | PostGIS / Dashboard / integrations | Continue improving precision and context handoffs |
| In-App Instructions | Working | High | Dashboard | Continue concise help on specialist/admin pages |
| Daily Manager Brief / What Changed v2 | Working | High | Dashboard / n8n / Postgres | Continue exception ranking and contextual drill-down |
| Commitments / Waiting On | Working | High | Dashboard / Postgres | Continue chase/follow-up ergonomics in Command Center |
| Additional Transit / Traffic / Port Authority Sources | Backlog | Medium | n8n / Postgres | Add after NJ Transit based on operational value |
| SMS / Email Delivery | Backlog | Low | Routing layer | Add only if needed |
| Obsidian / Local Documents | Deferred | Medium | Future knowledge layer | Revisit after active next-phase work stabilizes |
| Native Mobile App | Deferred | Low | Future | Web field experience remains default |

## Status Definitions

- **Working** - deployed and part of the current operating system.
- **Planned** - accepted next-phase work.
- **Backlog** - useful future enhancement after current priorities.
- **Deferred** - intentionally parked until higher-value work is stable.

## Core build status

The original GIS closeout sequence is complete:

1. **#8** - Hudson County parcel/address production foundation - COMPLETE.
2. **#9** - automated validated GIS refresh/promotion - COMPLETE.
3. **#10** - flood / geographic intelligence and Mapping Center - COMPLETE.

City Manager OS is now in production/maintenance mode for its original core scope. New work is prioritized by operational value and ease of municipal administration.
