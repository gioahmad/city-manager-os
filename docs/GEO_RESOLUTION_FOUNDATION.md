# Geo Resolution Foundation

## Runtime rule

City Manager OS resolves locations against local PostGIS datasets. NJ and NYC
government services are acquisition and refresh sources only. They are not
runtime dependencies and are not called for individual lookups.

## Shared resolution flow

1. Inspect the complete payload, including nested and source-specific fields.
2. Extract address, intersection, facility, corridor, place, reference and
   coordinate candidates.
3. Reject incident labels and other non-location text.
4. Rank candidates while preserving their original source paths.
5. Resolve against local datasets.
6. Cache the outcome with confidence, provenance and active dataset versions.
7. Attach the result to an existing entity through `geo_entity_resolutions`.
8. Retain unresolved and ambiguous records for later reprocessing.

BNN is intentionally handled as a fluid semi-structured source. No single BNN
field is assumed to contain its location.

## Active alert-resolution slice

The existing integration engine continuously processes bounded batches from
the existing `alerts` table. It passes the complete normalized alert, location,
metadata and raw payload into the shared resolver. There is no second ingestion
queue or notification path.

The resolver now provides supplied-coordinate resolution, exact and common
street-suffix NG911 address matching, addresses embedded inside longer BNN or
source messages, and low-confidence municipality or county centroids when no
precise point is available. Only resolutions at or above the precise confidence
threshold populate `alerts.geom`. Approximate points remain separately labeled
in entity provenance and are excluded from precise spatial matching.

Mapping Center exposes precise and approximate alerts, per-source coverage,
confidence, provenance and pending counts. A bounded local backfill uses the
same worker and cache as live resolution.

Intersection, facility and corridor resolution plug into this resolver when
the #58 reference catalog is added. NYC uses the same contracts when its local
reference datasets are loaded. There will be no parallel resolver, GIS
database or notification path.

## Cache invalidation

The cache key includes coordinates and the active `gis_dataset_versions`
records. Distinct supplied points cannot collide, and promoting a new dataset
version produces new resolution keys without deleting historical provenance.
