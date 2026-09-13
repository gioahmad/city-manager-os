# Regional Spatial Reference Catalog and Parcel Topology

Issue #58 adds reusable reference geography to the existing City Manager OS PostGIS database. It follows `SEE IT -> TRACK IT -> TELL ME` without creating a second GIS store, Watchlist, subscriber model, router, or notification path.

## Canonical catalog

`spatial_reference_entities` stores typed facilities, venues, corridors, landmarks, parcel references, service areas, and other operational geography. Each record keeps canonical names and aliases, full EPSG:4326 geometry, a point-on-surface centroid, source identity and provenance, confidence, importance, parcel and transit links, refresh timestamps, retirement state, and source metadata.

The first refresh reuses and links data already in PostGIS:

- Hudson County named facilities from statewide `gis_parcels`
- Hudson County landmark aliases from statewide `gis_landmark_aliases` and `gis_addresses`
- active mapped stationary `transit_assets` such as stops, stations, and terminals, linked by their existing UUID rather than copied into another transit model

Live vehicle positions remain in the existing Transit system and are deliberately excluded from the permanent reference catalog. This prevents moving vehicles from creating catalog churn while preserving them for live transit observations and impact context.

`spatial_reference_refresh_local_sources()` is idempotent. It upserts by stable source ID and retires missing linked records. Operators can run it from the Reference Catalog after an authoritative source promotion. A later lifecycle update can invoke the same function without creating another GIS refresh system.

NJ road centerline geometry and official NYC facilities/CSCL are not present locally, so the release does not invent corridor lines or Madison Square Garden coordinates. Existing Mapping Center lines and polygons can be adopted with provenance. Authoritative NJ road and NYC reference ingestion remains required for those acceptance items.

## Parcel topology

- `gis_parcel_for_point(lat, lon, tolerance_ft)` identifies the containing parcel or a parcel inside the explicit small boundary tolerance.
- `gis_addresses_for_parcel(parcel_objectid, tolerance_ft, limit)` returns active NG911 addresses linked by `pcl_guid`, point in parcel, or fuzzy boundary.
- `gis_adjoining_parcels(parcel_objectid, tolerance_ft, limit)` returns parcels sharing or nearly sharing a boundary.
- `gis_parcels_within_radius(parcel_objectid, radius_ft, limit, tolerance_ft)` returns adjoining and nearby parcels.
- `gis_parcels_within_radius(lat, lon, radius_ft, limit)` is the point-based overload and includes the containing parcel.
- `gis_spatial_impact_context(geometry, radius_ft, since)` reads the existing reference, Watchlist, Command Center, Alert, Event, Transit, and flood layers.
- `gis_parcel_context(parcel_objectid, radius_ft)` returns the source parcel, same-parcel addresses, adjoining parcels, nearby parcels, references, and the combined impact context.

All runtime geometry is EPSG:4326. Geography measurements are converted from meters and returned in feet. Adjacency uses an explicit three-foot default tolerance and exposes distance and shared-boundary measurements.

## Watch This

A reference is not automatically a watch. `Watch This` creates or updates one ordinary `watch_items` row and its existing `watch_item_recipients` routes. It supports:

- exact entity, adjoining-parcel union, or radius-buffer geometry
- start and expiration windows
- source and alert-category filters
- minimum priority and existing subscribers

The full selected geometry is retained in `watch_items.spatial_geom`; the existing point centroid remains in `watch_items.geom` for backward compatibility. Existing text/category matching, Subscribers, Routing, Delivery Guard, and ntfy remain unchanged. Activating live PostGIS proximity matches against `spatial_geom` belongs to #56 and is deliberately not hidden inside this database/UI release.

## Dashboard and APIs

- `/spatial-reference` searches, adopts, and refreshes reference records.
- `/spatial-reference/{entity_id}` shows provenance, parcel topology, nearby history, impact counts, and Watch This controls.
- `/api/spatial-reference/source-search` searches addresses, parcels, landmark aliases, transit assets, and custom Mapping Center features.
- `/api/spatial-reference/{entity_id}/nearby-history` returns the existing mapped operational history around a reference.
- `/api/spatial-reference/{entity_id}/impact-buffer.geojson` returns an auditable radius buffer.
- `/api/parcel/for-point` and `/api/parcels/within-radius` expose point-based topology.
- `/api/parcel/{objectid}/context`, `/addresses`, `/adjoining`, and `/nearby` expose parcel-based topology.
- `/map/system/spatial-references.geojson` supplies the viewport-aware Mapping Center layer.

No alert, delivery, subscriber, or notification records are created by installation or source refresh.
