#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  import_gis.sh parcels COUNTY
  import_gis.sh addresses COUNTY

Examples:
  import_gis.sh parcels HUDSON
  import_gis.sh addresses HUDSON

Imports an existing local GeoJSON snapshot into a PostGIS staging table.
This script does not download GIS data.
EOF
}

if [[ $# -ne 2 ]]; then
  usage
  exit 1
fi

DATASET="${1,,}"
COUNTY="${2^^}"
COUNTY_LOWER="${COUNTY,,}"

case "$DATASET" in
  parcels)
    SOURCE_FILE="/opt/citymanager-data/gis/raw/parcels/${COUNTY_LOWER}_parcels_mod4.geojson"
    TARGET_TABLE="stg_${COUNTY_LOWER}_parcels"
    GEOM_ARGS=(-nlt PROMOTE_TO_MULTI)
    ;;
  addresses)
    SOURCE_FILE="/opt/citymanager-data/gis/raw/addresses/${COUNTY_LOWER}_ng911_addresses.geojson"
    TARGET_TABLE="stg_${COUNTY_LOWER}_addresses"
    GEOM_ARGS=(-nlt POINT)
    ;;
  *)
    echo "ERROR: dataset must be 'parcels' or 'addresses'"
    exit 1
    ;;
esac

if [[ ! -f "$SOURCE_FILE" ]]; then
  echo "ERROR: source file not found: $SOURCE_FILE"
  exit 1
fi

if ! command -v ogr2ogr >/dev/null 2>&1; then
  echo "ERROR: ogr2ogr is not installed"
  exit 1
fi

POSTGIS_DIR="/opt/city-manager-os/deploy/postgis"
ENV_FILE="$POSTGIS_DIR/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: PostGIS .env not found: $ENV_FILE"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${POSTGRES_USER:?POSTGRES_USER missing from PostGIS .env}"
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD missing from PostGIS .env}"
: "${POSTGRES_DB:?POSTGRES_DB missing from PostGIS .env}"

PG_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{if .IPAddress}}{{.IPAddress}} {{end}}{{end}}' citymanager-postgis | awk '{print $1}')"

if [[ -z "$PG_IP" ]]; then
  echo "ERROR: unable to determine citymanager-postgis container IP"
  exit 1
fi

PG_DSN="PG:host=${PG_IP} port=5432 dbname=${POSTGRES_DB} user=${POSTGRES_USER}"

SOURCE_COUNT="$(ogrinfo -ro -so -al "$SOURCE_FILE" 2>/dev/null | awk -F': ' '/Feature Count:/ {print $2; exit}')"
SOURCE_COUNT="${SOURCE_COUNT:-unknown}"

printf '=== CITY MANAGER OS GIS STAGING IMPORT ===\n'
printf 'Dataset: %s\n' "$DATASET"
printf 'County: %s\n' "$COUNTY"
printf 'Source: %s\n' "$SOURCE_FILE"
printf 'Source features: %s\n' "$SOURCE_COUNT"
printf 'Target: public.%s\n' "$TARGET_TABLE"
printf 'CRS target: EPSG:4326\n\n'

PGPASSWORD="$POSTGRES_PASSWORD" \
OGR_PG_RETRIEVE_FID=YES \
ogr2ogr \
  -f PostgreSQL \
  "$PG_DSN" \
  "$SOURCE_FILE" \
  -nln "public.${TARGET_TABLE}" \
  -overwrite \
  -t_srs EPSG:4326 \
  -lco GEOMETRY_NAME=geom \
  -lco SPATIAL_INDEX=GIST \
  --config PG_USE_COPY YES \
  "${GEOM_ARGS[@]}"

DB_STATS="$(docker exec citymanager-postgis \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "
SELECT
  count(*)::text || '|' ||
  COALESCE(ST_SRID(geom)::text, 'NULL') || '|' ||
  COALESCE(string_agg(DISTINCT GeometryType(geom), ',' ORDER BY GeometryType(geom)), 'NULL')
FROM public.${TARGET_TABLE};
")"

IFS='|' read -r DB_COUNT DB_SRID DB_GEOMS <<< "$DB_STATS"

printf '\n=== IMPORT VALIDATION ===\n'
printf 'Database rows: %s\n' "$DB_COUNT"
printf 'SRID: %s\n' "$DB_SRID"
printf 'Geometry type(s): %s\n' "$DB_GEOMS"

if [[ "$SOURCE_COUNT" != "unknown" && "$DB_COUNT" != "$SOURCE_COUNT" ]]; then
  echo "ERROR: source/database row-count mismatch"
  exit 1
fi

if [[ "$DB_SRID" != "4326" ]]; then
  echo "ERROR: unexpected SRID: $DB_SRID"
  exit 1
fi

printf '\nGOOD: staging import completed successfully.\n'
