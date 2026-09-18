# GeoLibre migration boundary

Status: migration boundary for a later scoped release; not part of the basemap 403 recovery release.

## Why the 403 is repaired before changing map applications

The production Mapping Center uses OpenStreetMap raster tiles through Leaflet. The private dashboard sets a document-wide `Referrer-Policy: no-referrer`, while the OpenStreetMap tile service requires browser requests to include a valid Referer and may answer non-compliant traffic with HTTP 403.

GeoLibre is a map application and renderer, not a basemap host. Replacing Leaflet with GeoLibre without also choosing a reliable tile source would move the same dependency rather than remove it. The recovery release therefore applies Leaflet's tile-level `referrerPolicy` option only to requests for `tile.openstreetmap.org`, keeps the private policy for every other request, and leaves local operational layers usable when a basemap is unavailable.

## Target architecture

GeoLibre can replace the Mapping Center user interface only under these boundaries:

- Self-host a pinned GeoLibre web build on City Manager OS infrastructure.
- Serve it on the same origin and behind the existing private login.
- Keep PostgreSQL and PostGIS as the single system of record.
- Continue serving alerts, Watches, parcels, addresses, flood zones, spatial references, work items, events, and transit data from the existing authenticated City Manager OS endpoints.
- Keep the existing Geo Resolver for address, parcel, street, municipality, and map selections.
- Keep the Watchlist, Subscribers, Routing, Delivery Guard, and ntfy unchanged.
- Do not install GeoLens or create a second database, GIS catalog, Watch system, or notification system.
- Never expose database credentials to the browser.
- Disable public project sharing and hosted collaboration unless a later release explicitly provides a self-hosted, authenticated replacement.
- Prefer a locally served, licensed PMTiles or vector-tile basemap before retiring the current Mapping Center. A public community tile endpoint is not a production availability guarantee.

## Migration sequence

1. **Recover the current basemap.** Keep the private document policy, send an origin-only Referer on the OSM tile elements, and show a clear failure message while local layers continue to work.
2. **Run a read-only GeoLibre pilot.** Pin a reviewed GeoLibre release, serve it at a same-origin path, disable its sidecar and public sharing features, and load only existing City Manager OS endpoints.
3. **Prove operational parity.** Verify address and parcel search, alert shelf-life filters, visible-area search, global alert history, flood data, Watch locations, work items, event and transit layers, mobile layout, authentication, and sanitized errors.
4. **Add Watch creation parity.** A map point or drawn area must return to the existing five-step Watch workflow with the selected Location and one-mile default. GeoLibre must not write directly to Watch tables.
5. **Cut over reversibly.** Make GeoLibre the default only after acceptance passes. Keep the current Mapping Center available as a rollback path for one release cycle, then remove it separately.

## Acceptance gates

- No HTTP 403 from the selected production basemap during normal interactive use.
- No external host receives private addresses, Recipient details, Watch payloads, or database credentials.
- Existing map and Watch data remains unchanged.
- All operational layers remain session-protected and same-origin.
- GeoLibre can be disabled without a database rollback.
- The release runner builds and restarts only the services changed by the migration.

References: [GeoLibre self-hosting](https://geolibre.app/self-hosting/), [GeoLibre embedding](https://geolibre.app/user-guide/embedding/), and [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
