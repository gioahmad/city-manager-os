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
| Employee Photos / Checklists | Working | High | FastAPI / storage / Postgres | Maintain |
| Supervisor Verification | Working | High | Dashboard / Postgres | Maintain |
| Recurring Work Engine | Working | High | Operations Engine / Postgres | Configure real routines |
| Daily Awareness / Expected Activity | Working | High | Operations Engine / Dashboard | Configure real routines |
| Routine Start / End Dates | Working | Medium | Operations Engine / Postgres | Maintain |
| Occurrence-specific Today Notes | Working | Medium | Operations Engine / Dashboard | Maintain |
| GIS Parcel Downloader | Working | High | deploy/gis | Verify current Hudson snapshot |
| GIS Address Downloader | Working | High | deploy/gis | Verify current Hudson snapshot |
| GIS Staging Import | Working | High | deploy/gis / PostGIS | Run first Hudson imports |
| Production GIS Parcels | In Progress | High | PostGIS | Complete issue #8 |
| Production GIS Addresses | In Progress | High | PostGIS | Complete issue #8 |
| Block / Lot Enrichment | Pending GIS | Medium | PostGIS | Complete issue #8 |
| Nearby / Radius Search | Pending GIS | Medium | PostGIS | Complete issue #8 |
| Live Geographic Map | Pending GIS | High | Dashboard / PostGIS | Build after #8 |
| Automated GIS Refresh | Blocked | Medium | PostGIS / automation | Complete issue #9 after #8 |
| Flood Zones / Flood Intelligence | Planned | High | PostGIS / Dashboard / n8n | Complete issue #10 after GIS base |
| Additional Weather / Transit / Traffic Sources | Optional Expansion | Medium | n8n / Postgres | Add based on operational value |
| SMS / Email Delivery | Optional Expansion | Low | Routing layer | Add only if needed |
| Native Mobile App | Optional Expansion | Low | Future | Web experience is current default |

## Status Definitions

- **Working** — deployed and part of the current operating system.
- **In Progress** — implementation exists but production completion / validation remains.
- **Pending GIS** — depends on the first production parcel/address import.
- **Blocked** — intentionally waiting on a prerequisite issue.
- **Planned** — accepted remaining build work.
- **Optional Expansion** — useful future enhancement, not required for core closeout.

## Current closeout sequence

1. **#8** — complete and validate Hudson County parcel/address GIS production tables.
2. **#9** — automate validated GIS refresh/promotion.
3. **#10** — add flood / geographic intelligence and map behavior.

Everything else in the original core operating-system stack is now treated as production/maintenance rather than unfinished build work.
