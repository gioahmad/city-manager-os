#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
DATA_ROOT="/opt/citymanager-data/gis"
RUN_ID="$(date '+%Y%m%d%H%M%S')"
TMP_ROOT="${DATA_ROOT}/refresh/${RUN_ID}"
ARCHIVE_ROOT="${DATA_ROOT}/archive/${RUN_ID}"
LOCK_FILE="/var/lock/cmos-hudson-gis-refresh.lock"
MODE="${GIS_REFRESH_MODE:-full}"
MIN_RATIO="${GIS_REFRESH_MIN_RATIO:-0.80}"
MAX_RATIO="${GIS_REFRESH_MAX_RATIO:-1.25}"

PARCEL_NAME="hudson_parcels_mod4.geojson"
PARCEL_META_NAME="hudson_parcels_mod4.metadata.json"
ADDRESS_NAME="hudson_ng911_addresses.geojson"
ADDRESS_META_NAME="hudson_ng911_addresses.metadata.json"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }

for cmd in git docker python3 ogrinfo ogr2ogr flock; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another GIS refresh is already running; exiting without changes."
  exit 0
fi

cd "$REPO"

psql_cmd(){
  docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"' -- "$@"
}

mark_health(){
  local status="$1"
  local err="${2:-}"
  docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v status="$1" -v err="$2" -v run_id="$3" -Atc "
INSERT INTO source_health(source_id,status,last_attempt_at,last_success_at,last_error,metadata,updated_at)
VALUES (
  ''GIS_REFRESH'',
  :''status'',
  now(),
  CASE WHEN :''status''=''OK'' THEN now() ELSE NULL END,
  NULLIF(:''err'',''''),
  jsonb_build_object(''run_id'', :''run_id''),
  now()
)
ON CONFLICT (source_id) DO UPDATE
SET status=EXCLUDED.status,
    last_attempt_at=EXCLUDED.last_attempt_at,
    last_success_at=CASE WHEN EXCLUDED.status=''OK'' THEN EXCLUDED.last_success_at ELSE source_health.last_success_at END,
    last_error=EXCLUDED.last_error,
    metadata=EXCLUDED.metadata,
    updated_at=now();
"' -- "$status" "$err" "$RUN_ID" >/dev/null 2>&1 || true
}

refresh_failed(){
  local rc=$?
  local line="${BASH_LINENO[0]:-unknown}"
  local msg="Hudson GIS refresh failed at line ${line} with exit code ${rc}. Production GIS was retained unless a prior atomic promotion had already completed."
  mark_health "ERROR" "$msg"
  log "$msg"
  exit "$rc"
}
trap refresh_failed ERR

validate_snapshot(){
  local data_file="$1"
  local meta_file="$2"
  local label="$3"
  python3 - "$data_file" "$meta_file" "$label" <<'PY'
import hashlib, json, pathlib, sys

data = pathlib.Path(sys.argv[1])
meta_path = pathlib.Path(sys.argv[2])
label = sys.argv[3]
if not data.is_file() or data.stat().st_size <= 0:
    raise SystemExit(f"{label}: missing/empty data file: {data}")
if not meta_path.is_file():
    raise SystemExit(f"{label}: missing metadata: {meta_path}")
meta = json.loads(meta_path.read_text())
if meta.get("file") and meta["file"] != data.name:
    raise SystemExit(f"{label}: metadata filename mismatch")
expected = (meta.get("sha256") or "").lower()
h = hashlib.sha256()
with data.open("rb") as f:
    for block in iter(lambda: f.read(1024 * 1024), b""):
        h.update(block)
actual = h.hexdigest()
if expected and actual != expected:
    raise SystemExit(f"{label}: SHA256 mismatch")
count = int(meta.get("feature_count") or 0)
if count <= 0:
    raise SystemExit(f"{label}: invalid feature_count {count}")
if meta.get("crs") != "EPSG:4326":
    raise SystemExit(f"{label}: unexpected CRS {meta.get('crs')}")
print(f"{label}|{count}|{actual}|{meta.get('downloaded_at')}")
PY
}

sanity_check_count(){
  local label="$1" new_count="$2" old_count="$3"
  python3 - "$label" "$new_count" "$old_count" "$MIN_RATIO" "$MAX_RATIO" <<'PY'
import sys
label, new, old, lo, hi = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
if old <= 0:
    raise SystemExit(f"{label}: production baseline row count is not available")
ratio = new / old
print(f"{label}: new={new:,} production={old:,} ratio={ratio:.4f}")
if ratio < lo or ratio > hi:
    raise SystemExit(f"{label}: row-count ratio {ratio:.4f} outside allowed range {lo:.2f}-{hi:.2f}")
PY
}

log "Hudson GIS refresh run ${RUN_ID} starting in mode=${MODE}"

if [[ -n "$(git status --porcelain)" ]]; then
  git status --short
  fail "Repository is not clean. Automated refresh will not run against uncommitted code."
fi

if [[ "$MODE" == "validate" ]]; then
  log "Validation-only mode: checking current production and toolchain without downloads or writes"
  docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'gis_parcels' AS table_name,count(*) AS rows,
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_geom
FROM gis_parcels
UNION ALL
SELECT 'gis_addresses',count(*),
       count(*) FILTER (WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom))
FROM gis_addresses;
SELECT dataset_id,status,row_count,imported_at FROM gis_dataset_versions
WHERE dataset_id IN ('NJOGIS_HUDSON_PARCELS','NJOGIS_HUDSON_ADDRESSES')
ORDER BY dataset_id;
SQL
  log "HUDSON GIS REFRESH VALIDATION PASSED"
  exit 0
fi

[[ "$MODE" == "full" ]] || fail "GIS_REFRESH_MODE must be 'full' or 'validate'"

log "Synchronizing approved refresh code from origin/main"
git fetch origin main
git switch main
git pull --ff-only origin main
HEAD_SHA="$(git rev-parse HEAD)"
log "Using main @ ${HEAD_SHA}"

read -r PROD_P PROD_A < <(docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT (SELECT count(*) FROM gis_parcels), (SELECT count(*) FROM gis_addresses);"' | tr '|' ' ')
[[ "${PROD_P:-0}" =~ ^[0-9]+$ && "${PROD_A:-0}" =~ ^[0-9]+$ ]] || fail "Unable to read production GIS baseline counts"
(( PROD_P > 0 && PROD_A > 0 )) || fail "Production GIS baseline tables are empty"
log "Production baseline: parcels=${PROD_P}, addresses=${PROD_A}"

mkdir -p "$TMP_ROOT/parcels" "$TMP_ROOT/addresses" "$ARCHIVE_ROOT/parcels" "$ARCHIVE_ROOT/addresses"

log "Downloading fresh Hudson parcel snapshot to isolated refresh directory"
python3 deploy/gis/download_parcels.py --counties HUDSON --output-dir "$TMP_ROOT/parcels"
log "Downloading fresh Hudson NG911 address snapshot to isolated refresh directory"
python3 deploy/gis/download_addresses.py --counties HUDSON --output-dir "$TMP_ROOT/addresses"

PARCEL_DATA="$TMP_ROOT/parcels/$PARCEL_NAME"
PARCEL_META="$TMP_ROOT/parcels/$PARCEL_META_NAME"
ADDRESS_DATA="$TMP_ROOT/addresses/$ADDRESS_NAME"
ADDRESS_META="$TMP_ROOT/addresses/$ADDRESS_META_NAME"

log "Validating downloaded snapshots and checksums"
P_INFO="$(validate_snapshot "$PARCEL_DATA" "$PARCEL_META" "parcels")"
A_INFO="$(validate_snapshot "$ADDRESS_DATA" "$ADDRESS_META" "addresses")"
printf '%s\n%s\n' "$P_INFO" "$A_INFO"
IFS='|' read -r _ NEW_P P_SHA P_DOWNLOADED <<< "$P_INFO"
IFS='|' read -r _ NEW_A A_SHA A_DOWNLOADED <<< "$A_INFO"

log "Applying row-count sanity gates against current production"
sanity_check_count "parcels" "$NEW_P" "$PROD_P"
sanity_check_count "addresses" "$NEW_A" "$PROD_A"

log "Inspecting fresh source geometry summaries"
ogrinfo -ro -so -al "$PARCEL_DATA" | sed -n '1,35p'
ogrinfo -ro -so -al "$ADDRESS_DATA" | sed -n '1,35p'

archive_one(){
  local src="$1" dest="$2"
  [[ -e "$src" ]] || return 0
  if ! ln "$src" "$dest" 2>/dev/null; then
    cp -a "$src" "$dest"
  fi
}

log "Archiving current canonical raw snapshots before replacement"
archive_one "$DATA_ROOT/raw/parcels/$PARCEL_NAME" "$ARCHIVE_ROOT/parcels/$PARCEL_NAME"
archive_one "$DATA_ROOT/raw/parcels/$PARCEL_META_NAME" "$ARCHIVE_ROOT/parcels/$PARCEL_META_NAME"
archive_one "$DATA_ROOT/raw/addresses/$ADDRESS_NAME" "$ARCHIVE_ROOT/addresses/$ADDRESS_NAME"
archive_one "$DATA_ROOT/raw/addresses/$ADDRESS_META_NAME" "$ARCHIVE_ROOT/addresses/$ADDRESS_META_NAME"

install_snapshot(){
  local src="$1" dest="$2"
  local staged="${dest}.new.${RUN_ID}"
  cp --reflink=auto "$src" "$staged"
  mv -f "$staged" "$dest"
}

log "Atomically installing validated raw snapshots"
install_snapshot "$PARCEL_DATA" "$DATA_ROOT/raw/parcels/$PARCEL_NAME"
install_snapshot "$PARCEL_META" "$DATA_ROOT/raw/parcels/$PARCEL_META_NAME"
install_snapshot "$ADDRESS_DATA" "$DATA_ROOT/raw/addresses/$ADDRESS_NAME"
install_snapshot "$ADDRESS_META" "$DATA_ROOT/raw/addresses/$ADDRESS_META_NAME"

log "Loading fresh snapshots into staging"
bash deploy/gis/import_gis.sh parcels HUDSON
bash deploy/gis/import_gis.sh addresses HUDSON

log "Promoting validated staging through the proven production promotion path"
bash deploy/gis/promote_hudson_gis.sh

log "Verifying final production counts and dataset-version records"
docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
\pset pager off
SELECT 'PRODUCTION' AS state,
       (SELECT count(*) FROM gis_parcels) AS parcels,
       (SELECT count(*) FROM gis_addresses) AS addresses,
       (SELECT count(*) FROM gis_parcels WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_parcels,
       (SELECT count(*) FROM gis_addresses WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_addresses;
SELECT dataset_id,status,row_count,imported_at,notes
FROM gis_dataset_versions
WHERE dataset_id IN ('NJOGIS_HUDSON_PARCELS','NJOGIS_HUDSON_ADDRESSES')
ORDER BY dataset_id;
SQL

mark_health "OK" ""
log "HUDSON GIS REFRESH PASSED"
log "Fresh parcel snapshot: ${NEW_P} rows, sha256=${P_SHA}, downloaded=${P_DOWNLOADED}"
log "Fresh address snapshot: ${NEW_A} rows, sha256=${A_SHA}, downloaded=${A_DOWNLOADED}"
log "Raw archive retained permanently at: ${ARCHIVE_ROOT}"
log "Repository commit: ${HEAD_SHA}"
