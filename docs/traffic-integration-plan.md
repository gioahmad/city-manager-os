# Traffic intelligence: scheduled collection, shared display

Status: provider research and integration design; no traffic provider is connected or enabled by this UI change.

## Start with regional sources

- NJDOT's 511NJ publishes incidents and camera images. Confirm the current supported JSON endpoints and permission to redistribute snapshots before configuring a connector. Do not scrape undocumented camera URLs.
- 511NY offers a developer API and TRANSCOM regional events covering New York, New Jersey, and Connecticut. Request a developer key and filter the event feed to the local operating area. Camera coverage must be checked separately; NY camera availability does not establish NJ coverage.
- TomTom offers flow segment data: current speed, free-flow speed, travel times, confidence, and closures. Trial a small set of road segments after checking current pricing, display attribution, and caching rights.
- Google's traffic layer is a Google Maps display, not a reusable cached traffic dataset. Its caching restrictions and map usage charges make it unsuitable as the default shared snapshot source. Keep it optional and explicitly loaded if chosen.

Official references:
- https://www.nj.gov/transportation/commuter/511/web.shtm
- https://www.511ny.org/developers/resources
- https://511ny.org/developers/doc
- https://docs.tomtom.com/traffic-api/documentation/tomtom-maps/v1/traffic-flow/flow-segment-data
- https://developers.google.com/maps/documentation/javascript/policies

## Reuse the existing Python integration worker

`integration_worker.py` runs `run_due_integrations`; each integration already has `poll_seconds`. Collect from the worker, store the normalized snapshot in PostgreSQL, and make the dashboard read it. Page loads and the dashboard Refresh button must never trigger a traffic provider call. Avoid a new scheduling service or Redis dependency.

Proposed initial schedule (not enabled):

| Resource | Schedule | Maximum daily calls before retries |
|---|---|---:|
| One incidents endpoint | Every 10 minutes | 144 |
| Four flow segment requests | Every 15 minutes | 384 |
| Six camera snapshots | Every 30 minutes | 288 |
| One camera directory | Once a day | 1 |
| Total | Independent of viewer count | 817 |

This assumes one request per listed resource and no extra pagination or map-tile requests. Reserve 183 requests below an example 1,000/day ceiling. Enforce a database-backed per-provider daily cap across workers and restarts before any paid call; retries, manual tests, and pagination count against it. Provider billing may count units differently from HTTP requests, so reconcile the cap with the provider's actual SKU. At the cap, serve the last permitted snapshot with a stale label.

## Required implementation checks

- Persist next poll time, collection status, calls attempted today, and latest successful snapshot. A database claim/lock must prevent duplicate polls across worker instances. Failed attempts must not cause per-minute uncontrolled retries; honor Retry-After and capped backoff.
- Limit paid requests to configured roads and cameras. Do not proxy arbitrary URLs. Keep keys server-side and restrict outbound hosts.
- Show actual source, observation time, collection time, confidence where supplied, and stale/offline state. A camera is a periodically refreshed image unless a separately approved video stream is configured; do not label old frames live.
- Fetch camera images once centrally only where redistribution/storage is permitted. A directory listing is not a camera image request. Direct browser images or streams cause viewer-dependent network traffic and cannot meet a strict central polling cap.
- Render snapshots in Area intelligence and the existing map without changing base-map layers, watch matching, alert delivery, or recipients. Add no traffic-triggered notifications until explicitly configured.
- Public/TV output contains traffic snapshots only, never personal inbox, contact, or health content.
- Verify 1,000 dashboard visits produce zero additional provider requests; verify two workers claim one poll; verify retries and restart cannot bypass the daily cap; verify failure and exhausted quota display stale data.
