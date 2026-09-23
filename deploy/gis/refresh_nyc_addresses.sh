#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
MODE="${1:-refresh}"
URL="https://data.cityofnewyork.us/api/views/uf93-f8nk/rows.csv?accessType=DOWNLOAD"
DATA_DIR="/opt/citymanager-data/gis/nyc"
CSV="${DATA_DIR}/AddressPoint.csv"
LOCK_FILE="/var/lock/cmos-nyc-address-refresh.lock"
RUN_ID="NYC-$(date '+%Y%m%d%H%M%S')"
MIN_ADDRESSES=900000

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
db(){ docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
db_at(){ docker exec -i citymanager-postgis sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq'; }

trap 'rc=$?; log "NYC ADDRESS REFRESH FAILED with exit code ${rc}. Existing production data was retained unless atomic promotion completed."; exit $rc' ERR
[[ "$MODE" == refresh || "$MODE" == validate ]] || fail "mode must be refresh or validate"
for cmd in docker flock git ogr2ogr python3 sha256sum; do command -v "$cmd" >/dev/null || fail "$cmd is required"; done
exec 9>"$LOCK_FILE"
flock -n 9 || fail "another NYC address refresh is active"

cd "$REPO"
[[ "$(git branch --show-current)" == main ]] || fail "production checkout must be on main"
[[ -z "$(git status --porcelain)" ]] || fail "repository must be clean"
git fetch -q origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || fail "local main must match origin/main"

db < deploy/postgis/init/034_nyc_address_points.sql
if [[ "$MODE" == validate ]]; then
  db <<'SQL'
SELECT count(*) AS addresses,count(DISTINCT post_comm) AS boroughs,
       min(ST_SRID(geom)) AS min_srid,max(ST_SRID(geom)) AS max_srid
FROM gis_nyc_addresses;
SELECT dataset_id,status,row_count,imported_at
FROM gis_dataset_versions WHERE dataset_id='NYC_ADDRESSPOINT';
SQL
  log "NYC ADDRESS VALIDATION PASSED"
  exit 0
fi

install -d -m 0750 "$DATA_DIR"
TMP="${CSV}.part"
rm -f "$TMP"
log "Downloading official NYC Open Data AddressPoint"
python3 - "$URL" "$TMP" <<'PY'
import shutil, sys
from urllib.request import Request, urlopen
request = Request(sys.argv[1], headers={"User-Agent": "CityManagerOS/1.0"})
with urlopen(request, timeout=120) as source, open(sys.argv[2], "wb") as target:
    shutil.copyfileobj(source, target, length=1024 * 1024)
PY
mv "$TMP" "$CSV"
SHA="$(sha256sum "$CSV" | awk '{print $1}')"

set -a
# shellcheck disable=SC1091
source deploy/postgis/.env
set +a
: "${POSTGRES_USER:?}" "${POSTGRES_PASSWORD:?}" "${POSTGRES_DB:?}"
PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}} {{end}}{{end}}' citymanager-postgis | awk '{print $1}')"
[[ -n "$PG_IP" ]] || fail "unable to determine PostGIS container IP"
PG_DSN="PG:host=${PG_IP} port=5432 dbname=${POSTGRES_DB} user=${POSTGRES_USER}"

log "Loading NYC staging table"
PGPASSWORD="$POSTGRES_PASSWORD" ogr2ogr -f PostgreSQL "$PG_DSN" "$CSV" \
  -overwrite -nln public.stg_nyc_addresspoint_raw -oo GEOM_POSSIBLE_NAMES=the_geom \
  -a_srs EPSG:4326 -nlt POINT -lco GEOMETRY_NAME=geom --config PG_USE_COPY YES

log "Building and validating NYC address candidate"
db <<'SQL'
DROP TABLE IF EXISTS gis_nyc_addresses_build;
CREATE TABLE gis_nyc_addresses_build AS
SELECT objectid::bigint,
       "address point id"::text AS addresspointid,
       regexp_replace(trim(concat_ws(' ',"house number","house number suffix","full street name")),'\s+',' ','g') AS fulladdr,
       CASE "borough code" WHEN '1' THEN 'Manhattan' WHEN '2' THEN 'Bronx'
            WHEN '3' THEN 'Brooklyn' WHEN '4' THEN 'Queens' WHEN '5' THEN 'Staten Island' END AS post_comm,
       zipcode::text AS post_code,bin::text AS pcl_guid,ST_Force2D(geom)::geometry(Point,4326) AS geom,
       'A'::text AS status,'Y'::text AS primarypt,
       CASE "borough code" WHEN '1' THEN 'Manhattan' WHEN '2' THEN 'Bronx'
            WHEN '3' THEN 'Brooklyn' WHEN '4' THEN 'Queens' WHEN '5' THEN 'Staten Island' END AS inc_muni,
       'NY'::text AS state,
       CASE "borough code" WHEN '1' THEN 'New York' WHEN '2' THEN 'Bronx'
            WHEN '3' THEN 'Kings' WHEN '4' THEN 'Queens' WHEN '5' THEN 'Richmond' END AS county,
       nullif(modified_date,'')::timestamptz AS source_updated_at
FROM stg_nyc_addresspoint_raw
WHERE geom IS NOT NULL AND "borough code" IN ('1','2','3','4','5')
  AND nullif(trim("house number"),'') IS NOT NULL AND nullif(trim("full street name"),'') IS NOT NULL;
ALTER TABLE gis_nyc_addresses_build ADD PRIMARY KEY(objectid);
ALTER TABLE gis_nyc_addresses_build ALTER COLUMN addresspointid SET NOT NULL;
ALTER TABLE gis_nyc_addresses_build ALTER COLUMN fulladdr SET NOT NULL;
ALTER TABLE gis_nyc_addresses_build ALTER COLUMN post_comm SET NOT NULL;
ALTER TABLE gis_nyc_addresses_build ALTER COLUMN geom SET NOT NULL;
CREATE INDEX gis_nyc_addresses_build_geom_gix ON gis_nyc_addresses_build USING gist(geom);
CREATE INDEX gis_nyc_addresses_build_geog_gix ON gis_nyc_addresses_build USING gist((geom::geography));
CREATE INDEX gis_nyc_addresses_build_fulladdr_idx ON gis_nyc_addresses_build(lower(fulladdr));
CREATE INDEX gis_nyc_addresses_build_borough_idx ON gis_nyc_addresses_build(lower(post_comm));
ANALYZE gis_nyc_addresses_build;
SQL

STATS="$(db_at "SELECT count(*)||'|'||count(DISTINCT post_comm)||'|'||coalesce(min(ST_SRID(geom)),0)||'|'||coalesce(max(ST_SRID(geom)),0)||'|'||count(*) FILTER (WHERE fulladdr='246 90 ST' AND post_comm='Brooklyn') FROM gis_nyc_addresses_build")"
IFS='|' read -r COUNT BOROUGHS MIN_SRID MAX_SRID TARGET <<<"$STATS"
(( COUNT >= MIN_ADDRESSES )) || fail "unexpected NYC address count: $COUNT"
[[ "$BOROUGHS" == 5 && "$MIN_SRID" == 4326 && "$MAX_SRID" == 4326 ]] || fail "borough or SRID validation failed: $STATS"
(( TARGET >= 1 )) || fail "Brooklyn regression address 246 90 ST is missing"

log "Promoting NYC addresses atomically"
db <<SQL
BEGIN;
ALTER TABLE gis_nyc_addresses RENAME TO gis_nyc_addresses_old;
ALTER TABLE gis_nyc_addresses_build RENAME TO gis_nyc_addresses;
DROP TABLE gis_nyc_addresses_old;
ALTER TABLE gis_nyc_addresses RENAME CONSTRAINT gis_nyc_addresses_build_pkey TO gis_nyc_addresses_pkey;
ALTER INDEX gis_nyc_addresses_build_geom_gix RENAME TO gis_nyc_addresses_geom_gix;
ALTER INDEX gis_nyc_addresses_build_geog_gix RENAME TO gis_nyc_addresses_geog_gix;
ALTER INDEX gis_nyc_addresses_build_fulladdr_idx RENAME TO gis_nyc_addresses_fulladdr_idx;
ALTER INDEX gis_nyc_addresses_build_borough_idx RENAME TO gis_nyc_addresses_borough_idx;
GRANT SELECT ON gis_nyc_addresses TO citymanager_app;
INSERT INTO gis_dataset_versions(dataset_id,dataset_name,source_url,imported_at,row_count,status,notes)
VALUES ('NYC_ADDRESSPOINT','NYC Open Data AddressPoint','$URL',now(),$COUNT,'ACTIVE','sha256=$SHA run=$RUN_ID')
ON CONFLICT(dataset_id) DO UPDATE SET dataset_name=excluded.dataset_name,source_url=excluded.source_url,
 imported_at=excluded.imported_at,row_count=excluded.row_count,status=excluded.status,notes=excluded.notes;
DROP TABLE IF EXISTS stg_nyc_addresspoint_raw;
COMMIT;
SQL
log "NYC ADDRESS REFRESH PASSED rows=${COUNT} sha256=${SHA}"
