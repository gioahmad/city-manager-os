#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
PARCEL_FILE="/opt/citymanager-data/gis/raw/parcels/hudson_parcels_mod4.geojson"
PARCEL_META="/opt/citymanager-data/gis/raw/parcels/hudson_parcels_mod4.metadata.json"
ADDRESS_FILE="/opt/citymanager-data/gis/raw/addresses/hudson_ng911_addresses.geojson"
ADDRESS_META="/opt/citymanager-data/gis/raw/addresses/hudson_ng911_addresses.metadata.json"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
trap 'rc=$?; log "HUDSON GIS STAGING FAILED with exit code ${rc}. Review the log before retrying."; exit $rc' ERR

for cmd in git docker python3 ogrinfo ogr2ogr; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done

cd "$REPO"
log "Synchronizing repository with origin/main"
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean. No GIS changes were made."
fi
git fetch origin main
git switch main
git pull --ff-only origin main
HEAD_SHA="$(git rev-parse HEAD)"
log "Using main @ ${HEAD_SHA}"

[[ -s "$PARCEL_FILE" ]] || fail "Hudson parcel snapshot is missing: $PARCEL_FILE"
[[ -s "$PARCEL_META" ]] || fail "Hudson parcel metadata is missing: $PARCEL_META"

validate_snapshot() {
  local data_file="$1"
  local meta_file="$2"
  local label="$3"
  log "Validating ${label} snapshot metadata and checksum"
  python3 - "$data_file" "$meta_file" <<'PY'
import hashlib, json, pathlib, sys

data = pathlib.Path(sys.argv[1])
meta_path = pathlib.Path(sys.argv[2])
meta = json.loads(meta_path.read_text())
expected_file = meta.get("file")
if expected_file and expected_file != data.name:
    raise SystemExit(f"metadata file mismatch: {expected_file} != {data.name}")
expected_sha = (meta.get("sha256") or "").lower()
h = hashlib.sha256()
with data.open("rb") as f:
    for block in iter(lambda: f.read(1024 * 1024), b""):
        h.update(block)
actual_sha = h.hexdigest()
if expected_sha and actual_sha != expected_sha:
    raise SystemExit(f"SHA256 mismatch: expected {expected_sha}, got {actual_sha}")
print(f"dataset={meta.get('dataset')}")
print(f"county={meta.get('county')}")
print(f"feature_count={meta.get('feature_count')}")
print(f"crs={meta.get('crs')}")
print(f"downloaded_at={meta.get('downloaded_at')}")
print(f"sha256={actual_sha}")
print(f"size_bytes={data.stat().st_size}")
PY
}

validate_snapshot "$PARCEL_FILE" "$PARCEL_META" "Hudson parcel"

log "Inspecting parcel source summary"
ogrinfo -ro -so -al "$PARCEL_FILE" | sed -n '1,80p'

log "Importing Hudson parcels into staging"
bash deploy/gis/import_gis.sh parcels HUDSON

mkdir -p "$(dirname "$ADDRESS_FILE")"
if [[ -s "$ADDRESS_FILE" && -s "$ADDRESS_META" ]]; then
  log "Existing Hudson NG911 address snapshot found; validating before reuse"
  if validate_snapshot "$ADDRESS_FILE" "$ADDRESS_META" "Hudson NG911 address"; then
    log "Existing address snapshot is valid; download skipped"
  else
    fail "Existing address snapshot failed validation. Move it aside before retrying."
  fi
else
  log "Downloading Hudson NG911 address snapshot from NJOGIS"
  python3 deploy/gis/download_addresses.py --counties HUDSON
  [[ -s "$ADDRESS_FILE" ]] || fail "Address downloader completed without expected file: $ADDRESS_FILE"
  [[ -s "$ADDRESS_META" ]] || fail "Address downloader completed without expected metadata: $ADDRESS_META"
  validate_snapshot "$ADDRESS_FILE" "$ADDRESS_META" "Hudson NG911 address"
fi

log "Inspecting address source summary"
ogrinfo -ro -so -al "$ADDRESS_FILE" | sed -n '1,80p'

log "Importing Hudson addresses into staging"
bash deploy/gis/import_gis.sh addresses HUDSON

log "Verifying staging table counts, SRIDs and geometry types"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT
  'stg_hudson_parcels' AS dataset,
  count(*) AS rows,
  min(ST_SRID(geom)) AS srid,
  string_agg(DISTINCT GeometryType(geom), ',' ORDER BY GeometryType(geom)) AS geometry_types
FROM stg_hudson_parcels;

SELECT
  'stg_hudson_addresses' AS dataset,
  count(*) AS rows,
  min(ST_SRID(geom)) AS srid,
  string_agg(DISTINCT GeometryType(geom), ',' ORDER BY GeometryType(geom)) AS geometry_types
FROM stg_hudson_addresses;

SELECT table_name, ordinal_position, column_name, data_type
FROM information_schema.columns
WHERE table_schema='public'
  AND table_name IN ('stg_hudson_parcels','stg_hudson_addresses')
ORDER BY table_name, ordinal_position;
SQL

log "Confirming production GIS tables were not modified"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At' <<'SQL'
SELECT COALESCE(to_regclass('public.gis_parcels')::text,'gis_parcels:not-present');
SELECT COALESCE(to_regclass('public.gis_addresses')::text,'gis_addresses:not-present');
SQL

log "HUDSON GIS STAGING PASSED"
log "Repository commit: ${HEAD_SHA}"
log "Next step: review staging schemas, then build guarded production promotion + lookup tests."
