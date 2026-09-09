# GIS Refresh Resilience

The scheduled GIS refresh contacts ArcGIS only to update local snapshots. City
Manager OS runtime resolution continues to use local PostGIS exclusively.

The refresh workspace is persistent across failed runs. Each completed ArcGIS
batch is flushed, fsynced and recorded in a checkpoint. A retry resumes at the
first incomplete batch rather than discarding prior downloads.

Transient DNS, connection, timeout, JSON, ArcGIS and selected HTTP failures use
bounded exponential recovery. Operators can tune the limits with:

- `GIS_ARCGIS_ATTEMPTS`, default 20
- `GIS_ARCGIS_TIMEOUT_SECONDS`, default 180
- `GIS_ARCGIS_MAX_DELAY_SECONDS`, default 300

Canonical raw snapshots seed the workspace. When the county object IDs and
source edit revision match their recorded metadata, the existing snapshot is
reused. If both Hudson datasets are unchanged, staging and PostGIS promotion
are skipped.

Incomplete or incompatible checkpoints are renamed with a `.stale` timestamp
for inspection. Production tables remain untouched until both snapshots pass
checksum, row-count and geometry validation.
