# Unified Spatial Watch Pack

Issue #56 makes proximity a native matching mode of the existing Watchlist. It does not add another watch, routing, history, GIS, or notification subsystem.

## Runtime path

1. Mapping Center, the Regional Spatial Reference Catalog, or a trusted point supplies geometry.
2. A normal `watch_items` row stores the target snapshot, operational buffer, schedule, filters, and recipients.
3. PostGIS evaluates precise `alerts.geom` or exact numeric coordinates already present in the shared Standard Alert payload against active, in-window watches.
4. The central Watchlist Matcher chooses one match result per watch and deduplicates subscribers.
5. Existing Routing, Delivery Guard, and ntfy remain the only delivery path.

Alerts without trustworthy geometry remain eligible for existing text and field matching. They cannot produce a proximity match.

## Geometry and time

- Point, address, intersection, parcel, facility, corridor, and selected area watches use `spatial_target_geom` as the source snapshot.
- `spatial_geom` remains the Mapping Center preview and topology geometry.
- Radius and corridor buffers use feet converted to meters for PostGIS geography operations.
- Canonical reference watches continue to come from #58 and may use entity, radius, or adjoining-parcel scope.
- Schedules support future starts, custom ends, permanent watches, and the 1h through 7d presets in #56.

## Nearby history and impact

`gis_spatial_history` queries existing alerts and reuses `gis_spatial_impact_context` for references, parcels, Watchlist items, open work, events, transit, and flood context. It creates no history table and sends no notification.

The Mapping Center can preview a 500-foot point watch and render a custom alert impact buffer. Watch layers include the schedule state and intended subscriber names.

## Deployment

The #56 release runner applies the additive migration, publishes only the existing central matcher, builds the shared application image once, recreates only Dashboard, runs targeted tests plus one required secure E2E, and publishes a redacted result to the GitHub release-results branch.

Database behavior tests run inside a transaction and roll back all synthetic alerts and watches. The matcher installer backs up n8n and restores the prior published matcher if publication or validation fails.
