# Feature Matrix

| Feature | Status | Phase | Priority | Lives In | Next Action |
|---|---|---|---|---|---|
| n8n Automation Engine | Working | 1 | Critical | n8n | Keep as backend engine |
| ntfy Notifications | Working | 1 | Critical | ntfy | Connect central router |
| Master Watchlist | Designing | 1 | Critical | CSV → Postgres | Finalize schema |
| Standard Alert Schema | Designed | 1 | High | schemas | Finalize fields |
| Subscriber Directory | Designing | 1 | High | Postgres | Define table |
| PostgreSQL/PostGIS | Not Started | 1 | Critical | Server | Deploy |
| Central Watchlist Matcher | Designed | 2 | Critical | n8n | Build DB-backed matcher |
| Recipient Resolver | Designed | 2 | High | n8n/Postgres | Build deduplicated resolver |
| Delivery Logging | Not Started | 2 | High | Postgres | Define and build log |
| PSEG Outages | Partial/Working | 2 | High | n8n | Route into OS |
| Fire SDR | Partial | 5 | High | n8n/Raspberry Pi | Normalize output |
| GIS Parcels | Not Started | 4 | High | PostGIS | Import Hudson County |
| Address Points | Not Started | 4 | High | PostGIS | Import local dataset |
| Block/Lot Enrichment | Designed | 4 | Medium | PostGIS | Build lookup |
| Nearby Property Search | Designed | 4 | Medium | PostGIS | Build radius query |
| Flood Zones | Planned | 4/5 | High | PostGIS/Dashboard | Acquire/import layer |
| Live Dashboard | Planned | 3 | High | UI | Choose/build v0.1 |
| Live Map | Planned | 3 | High | UI/PostGIS | Initial layer design |
| Intelligence Feed | Planned | 3 | High | UI/Postgres | Define event model |
| New Issue Intake | Planned | 3 | High | UI/n8n | Build intake form |
| Source Health | Planned | 3 | Medium | Dashboard | Add heartbeat model |
| Weather/NWS | Planned | 5 | High | n8n | Identify normalized feed |
| Flood Gauges/Tides | Planned | 5 | High | n8n | Identify feeds |
| Traffic/NJ 511 | Planned | 5 | Medium | n8n | Identify feed |
| PATH/NJ Transit | Planned | 5 | Medium | n8n | Identify feeds |
| Port Authority | Planned | 5 | Medium | n8n | Identify feeds |
| Event Intelligence | Research/Design | 5 | Medium | n8n | Define collectors |
| Internal Issue Tracking | Planned | 3/5 | High | UI/Postgres | Define issue record |
| Tasks/Follow-up | Planned | 5 | Medium | UI/Postgres | Define workflow |

## Status Definitions

- **Working** — in production/use now
- **Partial/Working** — useful functionality exists but not integrated into the OS architecture
- **Designed** — architecture/logic is sufficiently defined to build
- **Designing** — still making structural decisions
- **Planned** — accepted feature, not yet designed in detail
- **Research/Design** — source or method still being evaluated
- **Not Started** — approved next-stage work with no implementation yet
