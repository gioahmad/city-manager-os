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

## Current slice

The first slice provides flexible candidate extraction, supplied-coordinate
resolution, exact local NG911 address resolution, cache and coverage records,
an internal Mapping Center resolution endpoint, and countywide Hudson Mapping
Center access.

Intersection, facility, corridor, statewide NJ and NYC resolution plug into
the same resolver as their local reference datasets are added. There will be
no parallel resolver, GIS database or notification path.

## Cache invalidation

The cache key includes the active `gis_dataset_versions` records. Promoting a
new dataset version therefore produces new resolution keys without deleting
the historical provenance of earlier results.
