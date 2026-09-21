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
source messages, approximate intersections from the closest address-point pair,
the nearest available NG911 address on a known street, and municipality or
county centroids. If a requested house number is missing, the resolver chooses
the address on that street with the smallest house-number difference. For a
street without a house number, it returns a real address point nearest the
street center. The fallback order is exact address, nearest intersection
address, nearest street address, municipality, then county. Free-text New Jersey
locality hints support sources such as BNN that omit a structured city field.

Only resolutions at or above the precise confidence threshold populate
`alerts.geom`. Lower-confidence points remain labeled with their precision in
entity provenance and Mapping Center. Spatial Watches continue to use the same
displayed effective alert point rather than a second location path.

Mapping Center exposes precise and approximate alerts, per-source coverage,
confidence, provenance and pending counts. A bounded local backfill uses the
same worker and cache as live resolution.

## Verify existing BNN alerts

The integration engine can run every stored BNN alert through the current
resolver without changing alerts, cache rows, entity resolutions, Watches or
deliveries:

```bash
docker exec -i citymanager-integration-engine \
  python geo_resolver.py audit --source BNN
```

The JSON report groups results by status, match type and spatial precision and
includes representative examples. `complete: true`, matching `selected` and
`total_available` counts, and `error_count: 0` prove that the full stored BNN
history was evaluated. The audit transaction is read-only and cache use is
disabled, so it tests the current resolver rather than accepting prior results.

## Operator corrections

An authenticated operator can select an Alert in Mapping Center and choose
`Correct Alert Location`. One input accepts a copied `latitude, longitude` pair
or a Google Maps URL containing coordinates. The same control can place the
point by clicking the map and refine it with a draggable preview marker.

Saving updates the existing Alert point and `geo_entity_resolutions` record as
`MANUAL_COORDINATE_CORRECTION`, retains the prior point and correction history,
and clears stale spatial match rows. Current alerts are queued for the existing
spatial Watch rematcher. No parallel location table, resolver, or notification
path is created.

Facility and canonical corridor resolution can plug into this resolver through
the #58 reference catalog. NYC uses the same contracts when its local reference
datasets are loaded. There is no parallel resolver, GIS database or
notification path.

## Cache invalidation

The cache key includes coordinates and the active `gis_dataset_versions`
records. Distinct supplied points cannot collide, and promoting a new dataset
version produces new resolution keys without deleting historical provenance.
