# Feature Matrix

_Last reconciled with production/main: September 4, 2026._

| Feature | Status | Priority | Lives In | Next Action |
|---|---|---:|---|---|
| n8n Automation Engine | Working | Critical | n8n | Keep as backend engine |
| PostgreSQL / PostGIS | Working | Critical | Server / PostGIS | Maintain / back up |
| Master Watchlist | Working | Critical | Postgres / Dashboard | Maintain |
| Standard Alert Schema | Working | High | schemas / n8n | Maintain compatibility |
| Subscriber Directory | Working | High | Postgres / Dashboard | Maintain |
| Central Watchlist Matcher | Working | Critical | n8n / Postgres | Maintain |
| Recipient Resolver | Working | High | n8n / Postgres | Maintain |
| Delivery Guard / Deduplication | Working | High | n8n / Postgres | Maintain |
| Delivery Logging / Audit | Working | High | Postgres / Dashboard | Maintain |
| ntfy Notifications | Working | Critical | ntfy / n8n | Maintain dynamic routing |
| Source Health | Working | High | Postgres / Dashboard | Maintain |
| PSEG Outages | Working | High | n8n / Postgres / Dashboard | Maintain |
| Multi-source Operational Alerts | Working | High | n8n / Postgres | Expand only when useful |
| Live Dashboard / Overview | Working | High | FastAPI / Postgres | Maintain |
| Alerts / Intelligence Feed | Working | High | Dashboard / Postgres | Maintain |
| Command Center / Issue Tracking | Working | Critical | Dashboard / Postgres | Maintain |
| My Day | Working | Critical | Dashboard / Postgres | Maintain |
| Schedule / Meeting Prep | Working | High | Dashboard / Postgres | Maintain |
| Decision Desk | Working | High | Dashboard / Postgres | Maintain |
| Visibility Queue | Working | Medium | Dashboard / Postgres | Maintain |
| Rules Center | Working | High | Dashboard / Postgres | Extend only as needed |
| Executive Assistant / Morning Brief | Working | High | n8n / Dashboard / Postgres | Maintain |
| Employee Operations Portal | Working | Critical | FastAPI / Postgres | Maintain |
| Supervisor Operations Board | Working | Critical | Dashboard / Postgres | Maintain |
| Employee Photos / Checklists | Working | High | FastAPI / storage / Postgres | Maintain inline viewer / explicit download |
| Supervisor Verification | Working | High | Dashboard / Postgres | Maintain |
| Recurring Work Engine | Working | High | Operations Engine / Postgres | Configure real routines |
| Daily Awareness / Expected Activity | Working | High | Operations Engine / Dashboard | Configure real routines |
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
| Mapping Center | Working | High | Dashboard / PostGIS | Expand web GIS controls as useful |
| System Map Layers | Working | High | Dashboard / PostGIS | Maintain flood / parcel / address / watch / issue layers |
| Custom GIS Layers | Working | Medium | Dashboard / PostGIS | Add styling / editing tools as useful |
| GeoJSON Import / Web Drawing | Working | Medium | Dashboard / PostGIS | Expand import/export later if needed |
| FEMA Flood Zones | Working | High | PostGIS / Dashboard | Maintain static refresh path |
| Flood Intelligence Dashboard | Working | High | Dashboard / PostGIS | Maintain |
| NOAA Tide / Water Level Monitor | Working | High | n8n / Postgres | Monitor 5-minute source health |
| NWS Flood / Coastal Alerts | Working | High | n8n / Postgres | Maintain official alert normalization |
| Flood Spatial Watch Context | Working | High | PostGIS / n8n | Add real watched facilities as configured |
| Flood Dynamic Alert Routing | Working | Critical | n8n / Postgres / ntfy | Maintain central matcher path |
| Additional Weather / Transit / Traffic Sources | Optional Expansion | Medium | n8n / Postgres | Add based on operational value |
| Web-based Admin Expansion | Optional Expansion | Medium | Dashboard | Move routine CLI maintenance into authenticated UI over time |
| SMS / Email Delivery | Optional Expansion | Low | Routing layer | Add only if needed |
| Native Mobile App | Optional Expansion | Low | Future | Web experience is current default |

## Status Definitions

- **Working** — deployed and part of the current operating system.
- **Optional Expansion** — useful future enhancement, not required for core closeout.

## Core build status

The original GIS closeout sequence is complete:

1. **#8** — Hudson County parcel/address production foundation — COMPLETE.
2. **#9** — automated validated GIS refresh/promotion — COMPLETE.
3. **#10** — flood / geographic intelligence and Mapping Center — COMPLETE.

City Manager OS is now in production/maintenance mode for its original core scope. New work should be prioritized by operational value rather than treated as unfinished foundation work.
