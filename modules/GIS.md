# GIS / Property Intelligence

## Goal

Provide local geographic context to alerts without depending on live upstream GIS queries during incidents.

## Architecture

```text
Public / County GIS Downloads
        ↓
Scheduled Update Workflow
        ↓
PostgreSQL + PostGIS
        ↓
CORE - GIS Enricher
        ↓
Alert / Dashboard / Watchlist
```

## Initial Local Layers

1. Hudson County parcels / MOD-IV attributes
2. Address points
3. Municipal boundaries
4. Flood zones
5. Critical facilities / watchlist points

Future layers may include building footprints, roads, zoning, stormwater, utilities, schools, hospitals, senior facilities, hydrants, and construction areas.

## Parcel Enrichment

Given an address or known block/lot, return normalized context such as:

```text
parcel_id
block
lot
qualifier
property_address
municipality
latitude
longitude
property_class
assessment fields where appropriate
```

## Nearby Search

Watch items may specify:

```text
nearby_enabled = true
radius_ft = 500
```

PostGIS can then identify nearby parcels/watch items and attach that context to the incident.

## Runtime Principle

Incident-time GIS queries should be local.

Internet activity should primarily occur in scheduled refresh workflows, not in the critical notification path.

## Refresh Strategy

Initial target: monthly refresh where the upstream dataset warrants it.

Recommended update pattern:

```text
Download
  ↓
Import into staging table
  ↓
Validate row count / required fields / geometry
  ↓
PASS? ── no → retain current production data + alert
  │
 yes
  ↓
Swap staging into production
  ↓
Rebuild/analyze indexes as needed
  ↓
Record dataset update status
```
