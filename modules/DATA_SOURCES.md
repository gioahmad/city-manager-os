# Data Sources

## Existing / Partial

### PSEG
Status: partial/working.

Current useful data includes customer outages, municipal status, ETR, outage start, jobs/circuits, and notification formatting.

**Next:** make PSEG the first complete source integrated into the database-backed watchlist/router architecture.

### Fire SDR
Status: partial.

Components include SDR/Raspberry Pi, audio capture, speech-to-text, and incident parsing.

**Next:** normalize final incident output into the Standard Alert Schema after the PSEG pattern is proven.

## Planned Sources

| Source | Domain | Notes |
|---|---|---|
| ORU | Utility | Outage intelligence |
| NWS | Weather | Watches/warnings/advisories |
| Flood gauges | Flood | Live levels/thresholds |
| Tides | Coastal/Flood | Waterfront context |
| NJ 511 | Traffic | Incidents/closures |
| NJ Transit | Transit | Service advisories |
| PATH | Transit | Service advisories |
| Port Authority | Regional | Tunnel/bridge/transportation |
| Events/Venues | Events | Regional impact intelligence |
| Air quality | Environment | Health/operations context |
| Road closures | Municipal/Traffic | Internal and external |
| Construction | Municipal | Planned disruptions |
| Internal intake | Municipal | Staff/elected/resident issues |

## Source Contract

Every source should eventually emit the same Standard Alert object.

Source-specific API fields and parsing stay in the source workflow. The router receives normalized data only.

## Source Health

Every production collector should eventually expose at least:

```text
source_id
last_attempt_at
last_success_at
status
record_count / event_count where useful
last_error
```

This feeds the dashboard Source Health view and allows ntfy alerts when critical collectors become stale.
