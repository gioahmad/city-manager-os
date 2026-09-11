# NJ Statewide GIS Bulk Import

The initial statewide load uses official NJOGIS bulk File Geodatabase archives. Runtime resolution remains local in PostGIS and makes no NJOGIS requests.

## Sources

- `Addr_NG911.gdb.zip`, layer `Addr_addressPoint`
- `parcels_MOD4_Statewide.gdb.zip`, layer `Cad_parcel_mod4`
- Supporting parcel-block, road-alias and landmark-alias layers from the same archives

The importer reads the ZIP files directly with GDAL. It does not require a second extracted copy.
NJOGIS parcel layers can contain curved `MultiSurface` features even when the advertised layer type is `MultiPolygon`. The import linearizes those curves and promotes single polygons before PostgreSQL applies its strict `MultiPolygon` type. It never skips failed features.

## Guarded phases

1. `validate` checks repository state, fixed SHA256 values, archive structure, layer presence and source counts. It performs no database writes.
2. `stage` imports all five layers into `stg_nj_*`, transforms spatial data to EPSG:4326, and validates exact counts, 21-county coverage, Hudson retention, SRIDs and geometry. Production remains unchanged. NG911 address county coverage uses the source's six-digit county codes, including Hudson code `882278`.
3. `promote` reruns the complete staging gate, repairs invalid parcel geometry only in the candidate, builds indexes, validates again, and atomically swaps production tables. Existing completed staging tables can therefore be safely reused after a validation-code correction.

The September 2026 source contains seven documented address exceptions: three valid Mercer County points use the legacy text value `Mercer County`, and four empty source records have no county, address or geometry. The importer retains these source records for row-count fidelity, records their coverage limitation, and rejects any unknown exception type or material increase.

A current, checksum-valid PostgreSQL backup is mandatory for promotion. Create it before staging so the backup contains the compact accepted Hudson production state rather than the much larger temporary statewide staging tables.

Existing Hudson production tables are renamed as timestamped backups and retained after promotion. Hudson dataset-version records are marked `SUPERSEDED`; statewide records become `ACTIVE`.

Promotion disables the old Hudson-only monthly timer. This prevents a later Hudson refresh from replacing the statewide tables.

## Monthly lifecycle

After the initial statewide load is accepted, `install_statewide_gis_refresh_timer.sh` installs the replacement monthly timer. It does not start a refresh during installation.

The lifecycle runs on the first Sunday of each month at 03:15 Eastern with up to 15 minutes of jitter:

1. Probe both official bulk sources and write a source manifest.
2. Reuse unchanged local ZIPs without downloading them.
3. Resume interrupted downloads into `.part` files when a source revision changed.
4. Retain the prior local source revision as `.previous`.
5. Create and verify a database backup only when source data changed.
6. Stage and validate all five statewide layers.
7. Retry staging once with retained prior archives if a new revision fails validation, retain the rejected revision for diagnosis, and force a fresh source comparison on the next run.
8. Atomically promote validated tables, then remove staging tables and all but the newest database rollback generation.
9. Record each run in `gis_refresh_runs` and use the existing alert, subscriber, delivery and ntfy route for completion or failure.

Rejected source revisions are deleted only after a later statewide promotion succeeds. The immediate prior accepted source revision and database table generation remain available for rollback.

The Mapping Center reads this run ledger and PostgreSQL COPY progress every 30 seconds, so status and active dataset row counts are visible from any authenticated login. Runtime address and parcel lookup remains local in PostGIS.
