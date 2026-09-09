#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
ADDRESS_ARCHIVE="${CMOS_NJ_ADDRESS_ARCHIVE:-/opt/citymanager-data/gis/incoming/Addr_NG911.gdb.zip}"
PARCEL_ARCHIVE="${CMOS_NJ_PARCEL_ARCHIVE:-/opt/citymanager-data/gis/incoming/parcels_MOD4_Statewide.gdb.zip}"
ADDRESS_SHA="${CMOS_NJ_ADDRESS_SHA256:-fc9ab33a52d341c382d7a83a232ec8630ed976f71665170dbe54060f0a83b2f8}"
PARCEL_SHA="${CMOS_NJ_PARCEL_SHA256:-c32b59d652cb55a7235340e710ae16f881e6043e3eb03426a190e7c7f4769512}"
MODE="${1:-validate}"
LOCK_FILE="/var/lock/cmos-nj-statewide-gis.lock"
MANIFEST_DIR="/opt/citymanager-data/gis/manifests"
RUN_ID="$(date '+%Y%m%d%H%M%S')"
MANIFEST="${MANIFEST_DIR}/nj-statewide-${RUN_ID}.json"
EXPECTED_COUNTIES=21
MIN_PARCELS=3000000
MIN_ADDRESSES=3000000
MIN_HUDSON_PARCELS=114644
MIN_HUDSON_ADDRESSES=175824

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
db(){ docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
db_at(){ docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At'; }
db_scalar(){ printf '%s\n' "$1" | docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq'; }

trap 'rc=$?; log "NJ STATEWIDE GIS IMPORT FAILED with exit code ${rc}. Existing production GIS tables were not replaced unless the atomic promotion had already committed."; exit $rc' ERR

[[ "$MODE" == "validate" || "$MODE" == "stage" || "$MODE" == "promote" ]] \
  || fail "mode must be validate, stage or promote"

for cmd in docker flock git ogr2ogr ogrinfo python3; do
  command -v "$cmd" >/dev/null || fail "$cmd is required"
done

exec 9>"$LOCK_FILE"
flock -n 9 || fail "another statewide GIS operation is running"

cd "$REPO"
[[ "$(git branch --show-current)" == "main" ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main must match origin/main"
HEAD_SHA="$(git rev-parse HEAD)"

[[ "$(systemctl is-active cmos-hudson-gis-refresh.service || true)" != "activating" ]] \
  || fail "Hudson GIS refresh is still activating"
! pgrep -af 'refresh_hudson_gis|download_addresses|download_parcels' >/dev/null \
  || fail "a batch GIS downloader is still running"

log "Validating official NJOGIS archives and fixed checksums"
ADDRESS_GDB="$(python3 deploy/gis/statewide_archive.py "$ADDRESS_ARCHIVE" --sha256 "$ADDRESS_SHA" --field gdb_root)"
PARCEL_GDB="$(python3 deploy/gis/statewide_archive.py "$PARCEL_ARCHIVE" --sha256 "$PARCEL_SHA" --field gdb_root)"
ADDRESS_SOURCE="/vsizip/${ADDRESS_ARCHIVE}/${ADDRESS_GDB}"
PARCEL_SOURCE="/vsizip/${PARCEL_ARCHIVE}/${PARCEL_GDB}"

layer_count(){
  ogrinfo -ro -so "$1" "$2" 2>/dev/null | awk -F': ' '/Feature Count:/ {print $2; exit}'
}

SOURCE_A="$(layer_count "$ADDRESS_SOURCE" Addr_addressPoint)"
SOURCE_P="$(layer_count "$PARCEL_SOURCE" Cad_parcel_mod4)"
SOURCE_B="$(layer_count "$PARCEL_SOURCE" Cad_pclblock)"
SOURCE_RA="$(layer_count "$ADDRESS_SOURCE" Tran_roadNameAlias)"
SOURCE_LA="$(layer_count "$ADDRESS_SOURCE" Addr_LandmarkAlias)"

[[ "$SOURCE_A" =~ ^[0-9]+$ && "$SOURCE_A" -ge "$MIN_ADDRESSES" ]] || fail "unexpected address source count: $SOURCE_A"
[[ "$SOURCE_P" =~ ^[0-9]+$ && "$SOURCE_P" -ge "$MIN_PARCELS" ]] || fail "unexpected parcel source count: $SOURCE_P"
[[ "$SOURCE_B" =~ ^[0-9]+$ && "$SOURCE_B" -gt 0 ]] || fail "unexpected parcel-block source count: $SOURCE_B"
[[ "$SOURCE_RA" =~ ^[0-9]+$ && "$SOURCE_RA" -gt 0 ]] || fail "unexpected road-alias source count: $SOURCE_RA"
[[ "$SOURCE_LA" =~ ^[0-9]+$ && "$SOURCE_LA" -gt 0 ]] || fail "unexpected landmark-alias source count: $SOURCE_LA"

log "Sources: parcels=${SOURCE_P}, addresses=${SOURCE_A}, blocks=${SOURCE_B}, road_aliases=${SOURCE_RA}, landmark_aliases=${SOURCE_LA}"

if [[ "$MODE" == "validate" ]]; then
  db <<'SQL'
\pset pager off
SELECT 'PRODUCTION' AS state,
       (SELECT count(*) FROM gis_parcels) AS parcels,
       (SELECT count(*) FROM gis_addresses) AS addresses,
       (SELECT count(*) FROM gis_parcels WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_parcels,
       (SELECT count(*) FROM gis_addresses WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) AS bad_addresses;
SELECT dataset_id,status,row_count,imported_at
FROM gis_dataset_versions
WHERE dataset_id LIKE 'NJOGIS_%'
ORDER BY dataset_id;
SQL
  log "NJ STATEWIDE GIS VALIDATION PASSED"
  exit 0
fi

POSTGIS_DIR="$REPO/deploy/postgis"
set -a
# shellcheck disable=SC1091
source "$POSTGIS_DIR/.env"
set +a
: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}"
PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}} {{end}}{{end}}' citymanager-postgis | awk '{print $1}')"
[[ -n "$PG_IP" ]] || fail "unable to determine PostGIS container IP"
PG_DSN="PG:host=${PG_IP} port=5432 dbname=${POSTGRES_DB} user=${POSTGRES_USER}"

if [[ "$MODE" == "stage" ]]; then
  AVAILABLE_BYTES="$(df --output=avail -B1 /opt/citymanager-data | tail -n 1 | tr -d ' ')"
  (( AVAILABLE_BYTES >= 37580963840 )) || fail "at least 35 GiB free is required for statewide staging and candidate indexes"
  log "Importing statewide layers directly from compressed FileGDB archives"
  common=( -f PostgreSQL "$PG_DSN" -overwrite -progress -preserve_fid -lco FID=objectid -lco SPATIAL_INDEX=NONE --config PG_USE_COPY YES )
  PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr "${common[@]}" "$PARCEL_SOURCE" Cad_parcel_mod4 \
    -nln public.stg_nj_parcels -t_srs EPSG:4326 -dim XY -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom
  PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr "${common[@]}" "$ADDRESS_SOURCE" Addr_addressPoint \
    -nln public.stg_nj_addresses -t_srs EPSG:4326 -dim XY -nlt POINT -lco GEOMETRY_NAME=geom
  PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr "${common[@]}" "$PARCEL_SOURCE" Cad_pclblock \
    -nln public.stg_nj_parcel_blocks -t_srs EPSG:4326 -dim XY -nlt PROMOTE_TO_MULTI -lco GEOMETRY_NAME=geom
  PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr "${common[@]}" "$ADDRESS_SOURCE" Tran_roadNameAlias \
    -nln public.stg_nj_road_aliases -nlt NONE
  PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr "${common[@]}" "$ADDRESS_SOURCE" Addr_LandmarkAlias \
    -nln public.stg_nj_landmark_aliases -nlt NONE

  log "Validating staging counts, statewide coverage, Hudson retention and geometry"
  STATS="$(db_at <<SQL
WITH s AS (
 SELECT (SELECT count(*) FROM stg_nj_parcels) p,
        (SELECT count(*) FROM stg_nj_addresses) a,
        (SELECT count(*) FROM stg_nj_parcel_blocks) b,
        (SELECT count(*) FROM stg_nj_road_aliases) ra,
        (SELECT count(*) FROM stg_nj_landmark_aliases) la,
        (SELECT count(DISTINCT upper(trim(county))) FROM stg_nj_parcels WHERE nullif(trim(county),'') IS NOT NULL) pc,
        (SELECT count(DISTINCT upper(trim(county))) FROM stg_nj_addresses WHERE nullif(trim(county),'') IS NOT NULL) ac,
        (SELECT count(*) FROM stg_nj_parcels WHERE upper(trim(county))='HUDSON') hp,
        (SELECT count(*) FROM stg_nj_addresses WHERE upper(trim(county))='HUDSON') ha,
        (SELECT count(*) FROM stg_nj_parcels WHERE geom IS NULL OR ST_IsEmpty(geom)) pempty,
        (SELECT count(*) FROM stg_nj_parcels WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND NOT ST_IsValid(geom)) pinvalid,
        (SELECT count(*) FROM stg_nj_addresses WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom)) abad,
        (SELECT min(ST_SRID(geom)) FROM stg_nj_parcels) pmin,
        (SELECT max(ST_SRID(geom)) FROM stg_nj_parcels) pmax,
        (SELECT min(ST_SRID(geom)) FROM stg_nj_addresses) amin,
        (SELECT max(ST_SRID(geom)) FROM stg_nj_addresses) amax
)
SELECT p||'|'||a||'|'||b||'|'||ra||'|'||la||'|'||pc||'|'||ac||'|'||hp||'|'||ha||'|'||pempty||'|'||pinvalid||'|'||abad||'|'||pmin||'|'||pmax||'|'||amin||'|'||amax FROM s;
SQL
)"
  IFS='|' read -r DB_P DB_A DB_B DB_RA DB_LA PC AC HP HA PEMPTY PINVALID ABAD PMIN PMAX AMIN AMAX <<< "$STATS"
  [[ "$DB_P" == "$SOURCE_P" && "$DB_A" == "$SOURCE_A" && "$DB_B" == "$SOURCE_B" && "$DB_RA" == "$SOURCE_RA" && "$DB_LA" == "$SOURCE_LA" ]] || fail "source/staging row-count mismatch: $STATS"
  [[ "$PC" == "$EXPECTED_COUNTIES" && "$AC" == "$EXPECTED_COUNTIES" ]] || fail "expected 21 counties; parcels=${PC}, addresses=${AC}"
  (( HP >= MIN_HUDSON_PARCELS && HA >= MIN_HUDSON_ADDRESSES )) || fail "Hudson retention gate failed: parcels=${HP}, addresses=${HA}"
  [[ "$PMIN" == 4326 && "$PMAX" == 4326 && "$AMIN" == 4326 && "$AMAX" == 4326 ]] || fail "unexpected staging SRID range"
  (( PEMPTY == 0 && ABAD == 0 )) || fail "null/empty/invalid geometry found: parcel_empty=${PEMPTY}, address_bad=${ABAD}"

  install -d -m 0750 "$MANIFEST_DIR"
  python3 - "$MANIFEST" "$RUN_ID" "$HEAD_SHA" "$ADDRESS_ARCHIVE" "$ADDRESS_SHA" "$SOURCE_A" "$PARCEL_ARCHIVE" "$PARCEL_SHA" "$SOURCE_P" "$SOURCE_B" "$SOURCE_RA" "$SOURCE_LA" "$PINVALID" <<'PY'
import json, pathlib, sys
(path, run, head, aa, ash, ac, pa, psh, pc, blocks, roads, landmarks, invalid) = sys.argv[1:]
payload = {
    "run_id": run, "repository": head, "status": "STAGED",
    "addresses": {"archive": aa, "sha256": ash, "rows": int(ac)},
    "parcels": {"archive": pa, "sha256": psh, "rows": int(pc)},
    "parcel_blocks": int(blocks), "road_aliases": int(roads),
    "landmark_aliases": int(landmarks), "invalid_parcels_to_repair": int(invalid),
}
pathlib.Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
print(path)
PY
  log "NJ STATEWIDE GIS STAGING PASSED"
  log "Manifest: ${MANIFEST}"
  exit 0
fi

log "Promote mode requires already validated staging tables"
"$REPO/deploy/postgis/verify-backup.sh"
for table in stg_nj_parcels stg_nj_addresses stg_nj_parcel_blocks stg_nj_road_aliases stg_nj_landmark_aliases; do
  [[ "$(db_scalar "SELECT to_regclass('public.${table}') IS NOT NULL")" == t ]] || fail "missing staging table: $table"
done

BUILD_ID="$RUN_ID"
P="gis_parcels_build_${BUILD_ID}"
A="gis_addresses_build_${BUILD_ID}"
B="gis_parcel_blocks_build_${BUILD_ID}"
RA="gis_road_aliases_build_${BUILD_ID}"
LA="gis_landmark_aliases_build_${BUILD_ID}"

log "Building, repairing and indexing statewide production candidates"
db <<SQL
CREATE TABLE ${P} AS TABLE stg_nj_parcels;
UPDATE ${P} SET geom=ST_Multi(ST_CollectionExtract(ST_MakeValid(geom),3))
WHERE geom IS NOT NULL AND NOT ST_IsEmpty(geom) AND NOT ST_IsValid(geom);
CREATE TABLE ${A} AS TABLE stg_nj_addresses;
CREATE TABLE ${B} AS TABLE stg_nj_parcel_blocks;
CREATE TABLE ${RA} AS TABLE stg_nj_road_aliases;
CREATE TABLE ${LA} AS TABLE stg_nj_landmark_aliases;

CREATE INDEX ${P}_geom_gix ON ${P} USING GIST(geom);
CREATE INDEX ${P}_objectid_idx ON ${P}(objectid);
CREATE INDEX ${P}_pcl_guid_idx ON ${P}(pcl_guid) WHERE pcl_guid IS NOT NULL;
CREATE INDEX ${P}_pams_pin_idx ON ${P}(pams_pin) WHERE pams_pin IS NOT NULL;
CREATE INDEX ${P}_gis_pin_idx ON ${P}(gis_pin) WHERE gis_pin IS NOT NULL;
CREATE INDEX ${P}_block_lot_idx ON ${P}(pcl_mun,pclblock,pcllot);
CREATE INDEX ${P}_county_idx ON ${P}(upper(trim(county)));
CREATE INDEX ${P}_mun_name_idx ON ${P}(lower(mun_name));
CREATE INDEX ${P}_prop_loc_idx ON ${P}(lower(prop_loc));

CREATE INDEX ${A}_geom_gix ON ${A} USING GIST(geom);
CREATE INDEX ${A}_geog_gix ON ${A} USING GIST((geom::geography));
CREATE INDEX ${A}_objectid_idx ON ${A}(objectid);
CREATE INDEX ${A}_status_idx ON ${A}(status);
CREATE INDEX ${A}_pcl_guid_idx ON ${A}(pcl_guid) WHERE pcl_guid IS NOT NULL;
CREATE INDEX ${A}_county_idx ON ${A}(upper(trim(county)));
CREATE INDEX ${A}_fulladdr_idx ON ${A}(lower(fulladdr));
CREATE INDEX ${A}_post_comm_idx ON ${A}(lower(post_comm));
CREATE INDEX ${A}_post_code_idx ON ${A}(post_code);

CREATE INDEX ${B}_geom_gix ON ${B} USING GIST(geom);
CREATE INDEX ${B}_mun_block_idx ON ${B}(mun,block);
CREATE INDEX ${RA}_rcl_idx ON ${RA}(rcl_nguid);
CREATE INDEX ${RA}_prime_name_idx ON ${RA}(lower(ast_pname));
CREATE INDEX ${RA}_legacy_name_idx ON ${RA}(lower(alst_pname));
CREATE INDEX ${LA}_site_idx ON ${LA}(site_nguid);
CREATE INDEX ${LA}_name_idx ON ${LA}(lower(aclandmark));

ANALYZE ${P}; ANALYZE ${A}; ANALYZE ${B}; ANALYZE ${RA}; ANALYZE ${LA};
SQL

FINAL="$(db_at <<SQL
SELECT
 (SELECT count(*) FROM ${P})||'|'||(SELECT count(*) FROM ${A})||'|'||
 (SELECT count(*) FROM ${P} WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom))||'|'||
 (SELECT count(*) FROM ${A} WHERE geom IS NULL OR ST_IsEmpty(geom) OR NOT ST_IsValid(geom))||'|'||
 (SELECT count(DISTINCT upper(trim(county))) FROM ${P})||'|'||
 (SELECT count(DISTINCT upper(trim(county))) FROM ${A});
SQL
)"
IFS='|' read -r FINAL_P FINAL_A FINAL_PBAD FINAL_ABAD FINAL_PC FINAL_AC <<< "$FINAL"
[[ "$FINAL_P" == "$SOURCE_P" && "$FINAL_A" == "$SOURCE_A" ]] || fail "candidate count mismatch: $FINAL"
(( FINAL_PBAD == 0 && FINAL_ABAD == 0 )) || fail "candidate geometry validation failed: $FINAL"
[[ "$FINAL_PC" == "$EXPECTED_COUNTIES" && "$FINAL_AC" == "$EXPECTED_COUNTIES" ]] || fail "candidate county validation failed: $FINAL"

log "Atomically promoting statewide tables and retaining prior production backups"
log "Disabling the Hudson-only timer so it cannot replace statewide production on its next run"
systemctl disable --now cmos-hudson-gis-refresh.timer
db <<SQL
BEGIN;
DO \$\$
DECLARE
  item text;
BEGIN
  FOREACH item IN ARRAY ARRAY['gis_parcels','gis_addresses','gis_parcel_blocks','gis_road_aliases','gis_landmark_aliases']
  LOOP
    IF to_regclass('public.' || item) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE public.%I RENAME TO %I',item,item || '_backup_${BUILD_ID}');
    END IF;
  END LOOP;
END
\$\$;
ALTER TABLE ${P} RENAME TO gis_parcels;
ALTER TABLE ${A} RENAME TO gis_addresses;
ALTER TABLE ${B} RENAME TO gis_parcel_blocks;
ALTER TABLE ${RA} RENAME TO gis_road_aliases;
ALTER TABLE ${LA} RENAME TO gis_landmark_aliases;

UPDATE gis_dataset_versions SET status='SUPERSEDED'
WHERE dataset_id IN ('NJOGIS_HUDSON_PARCELS','NJOGIS_HUDSON_ADDRESSES');
INSERT INTO gis_dataset_versions(dataset_id,dataset_name,source_url,imported_at,row_count,status,notes)
VALUES
 ('NJOGIS_NJ_STATEWIDE_PARCELS','NJOGIS NJ Statewide Parcels / MOD-IV','https://geoapps.nj.gov/njgin/parcel/parcels_MOD4_Statewide.gdb.zip',now(),${FINAL_P},'ACTIVE','Bulk FileGDB promotion ${BUILD_ID}; SHA256 ${PARCEL_SHA}; repository ${HEAD_SHA}'),
 ('NJOGIS_NJ_STATEWIDE_ADDRESSES','NJOGIS NJ Statewide NG911 Address Points','https://geoapps.nj.gov/njgin/address/Addr_NG911.gdb.zip',now(),${FINAL_A},'ACTIVE','Bulk FileGDB promotion ${BUILD_ID}; SHA256 ${ADDRESS_SHA}; repository ${HEAD_SHA}')
ON CONFLICT(dataset_id) DO UPDATE SET dataset_name=excluded.dataset_name,source_url=excluded.source_url,imported_at=excluded.imported_at,row_count=excluded.row_count,status=excluded.status,notes=excluded.notes;

INSERT INTO source_health(source_id,status,last_attempt_at,last_success_at,last_error,metadata,updated_at)
VALUES('GIS_REFRESH','OK',now(),now(),NULL,jsonb_build_object('run_id','${BUILD_ID}','mode','NJ_STATEWIDE_BULK','repository','${HEAD_SHA}'),now())
ON CONFLICT(source_id) DO UPDATE SET status='OK',last_attempt_at=now(),last_success_at=now(),last_error=NULL,metadata=excluded.metadata,updated_at=now();
COMMIT;
SQL

db <<'SQL'
\pset pager off
SELECT 'PRODUCTION' state,(SELECT count(*) FROM gis_parcels) parcels,(SELECT count(*) FROM gis_addresses) addresses,
 (SELECT count(DISTINCT upper(trim(county))) FROM gis_parcels) parcel_counties,
 (SELECT count(DISTINCT upper(trim(county))) FROM gis_addresses) address_counties;
SELECT dataset_id,status,row_count,imported_at FROM gis_dataset_versions WHERE dataset_id LIKE 'NJOGIS_%' ORDER BY dataset_id;
SQL
log "NJ STATEWIDE GIS PRODUCTION PROMOTION PASSED"
log "Previous Hudson tables retained as gis_parcels_backup_${BUILD_ID} and gis_addresses_backup_${BUILD_ID}"
log "Hudson-only monthly timer is disabled pending installation of the statewide refresh timer"
