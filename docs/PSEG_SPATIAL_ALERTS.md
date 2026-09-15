# PSEG Spatial + NJ Statewide Alerts

Issue #60 consolidates PSEG into one City Manager OS path:

`PSEG ingestion → Alerts → Geo Resolver → Mapping Center → Watchlist → Subscribers → Routing → Delivery Guard → ntfy`

No second database, geocoder, map, watchlist, subscriber registry, router, delivery guard, or notification sender is introduced.

## Runtime behavior

- The existing integration engine polls the established PSEG municipal summary every 15 minutes.
- The first successful poll is a silent baseline. Invalid or incomplete municipal summaries preserve the last good state. A temporary provider-map failure degrades to retained or municipality-level local context without stopping outage counts and change alerts.
- Hudson municipal alerts retain their existing change/restoration behavior and gain approximate provider-map coordinates plus nearby context from local PostGIS.
- Provider map clusters are outage areas, not customer locations. Alert copy, metadata, and Mapping Center features say **approximate** and **not customer-specific**.
- Local enrichment prefers a nearby cataloged facility/corridor, then a street corridor or parcel area, then municipality fallback. No external geocoder runs per alert.
- NJ statewide notifications always exclude Hudson County. They are grouped into one compact county/municipality message.
- Only threshold crossings, configured material increases, restorations, and configured reminders create a statewide notification. Unchanged polls are suppressed.
- Qualifying statewide municipalities can be mapped as display-only alerts. Resolved or below-threshold map alerts are retained for Nearby History.
- Alerts marked for delivery are drained by one n8n workflow into the existing central Watchlist Matcher. Delivery Guard remains the final duplicate barrier before ntfy.

## Web controls

Open **Integrations → PSEG Spatial + NJ Statewide Alerts**.

- enable or pause statewide notifications;
- Major (2,000), Standard (500), Expanded (100), or Custom customer thresholds;
- optional percent threshold;
- county selection (empty means all supported counties; Hudson is never selectable);
- material customer increase;
- restoration notices;
- reminder interval (`0` disables reminders);
- statewide Mapping Center display.

Statewide delivery defaults to paused. Hudson monitoring stays independent and active. A policy change takes effect on the next 15-minute poll.

## Deployment

The issue #60 release runner is pinned to its accepted base and exact candidate. It performs one additive migration, at most one cached shared-image build, restarts only Dashboard and Integration Engine when required, publishes one PSEG router, and restarts n8n only when that workflow changes. It does not restart PostGIS, Staff, Operations Engine, or ntfy and does not run the full E2E suite.

Every prior live PSEG workflow is exported to a mode-600 VPS backup before consolidation. A complete redacted definition review (nodes, connections, settings, original hash, and active state) is committed to `release-output/60` before any workflow is changed. The complete release log remains mode 600 on the VPS; a redacted result and error summary is pushed to the same branch automatically.
