#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="/opt/city-manager-os"
POSTGIS_ENV="$ROOT/deploy/postgis/.env"
DASH_ENV="$ROOT/dashboard/.env"
LABEL="${1:-CURRENT}"
TMP="$(mktemp -d /tmp/cmos-db-alignment.XXXXXX)"
CONTAINER_EXPORT="/tmp/cmos-db-alignment-$$.json"
HOST_EXPORT="$TMP/n8n-credentials.json"

cleanup(){
  set +e
  docker exec n8n rm -f "$CONTAINER_EXPORT" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

[[ -f "$POSTGIS_ENV" ]] || { echo "ERROR: missing $POSTGIS_ENV"; exit 1; }
[[ -f "$DASH_ENV" ]] || { echo "ERROR: missing $DASH_ENV"; exit 1; }

python3 - "$POSTGIS_ENV" "$DASH_ENV" >"$TMP/current.txt" <<'PY'
from pathlib import Path
import sys


def read_env(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in Path(path).read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith('#') or '=' not in raw:
            continue
        key, value = raw.split('=', 1)
        values[key.strip()] = value.strip()
    return values

postgis = read_env(sys.argv[1])
dashboard = read_env(sys.argv[2])

pg_password = postgis.get('POSTGRES_PASSWORD', '')
dash_password = dashboard.get('DB_PASSWORD', '')
user = postgis.get('POSTGRES_USER', '')
database = postgis.get('POSTGRES_DB', '')

if not pg_password:
    raise SystemExit('POSTGRES_PASSWORD missing')
if not dash_password:
    raise SystemExit('DB_PASSWORD missing')
if pg_password != dash_password:
    raise SystemExit('PostGIS and dashboard passwords differ')
if not user:
    raise SystemExit('POSTGRES_USER missing')
if not database:
    raise SystemExit('POSTGRES_DB missing')

Path('/dev/stdout').write_text(user + '\n' + database + '\n' + pg_password + '\n')
PY

mapfile -t CURRENT <"$TMP/current.txt"
DB_USER="${CURRENT[0]:-}"
DB_NAME="${CURRENT[1]:-}"
DB_PASSWORD_VALUE="${CURRENT[2]:-}"

[[ -n "$DB_USER" && -n "$DB_NAME" && -n "$DB_PASSWORD_VALUE" ]] || {
  echo "ERROR: could not resolve current database credential"
  exit 1
}

docker exec -u node n8n \
  n8n export:credentials \
  --all \
  --decrypted \
  --output="$CONTAINER_EXPORT" \
  >/dev/null

docker cp "n8n:$CONTAINER_EXPORT" "$HOST_EXPORT" >/dev/null
chmod 600 "$HOST_EXPORT"

python3 - "$HOST_EXPORT" "$DB_USER" "$DB_NAME" "$DB_PASSWORD_VALUE" <<'PY'
import json
from pathlib import Path
import sys

source, db_user, db_name, expected_password = sys.argv[1:]
credentials = json.loads(Path(source).read_text())
if not isinstance(credentials, list):
    raise SystemExit('n8n credential export is not a list')

matches: list[str] = []
for item in credentials:
    if item.get('type') != 'postgres':
        continue
    data = item.get('data')
    if not isinstance(data, dict):
        continue

    host = str(data.get('host') or '').strip()
    database = str(data.get('database') or data.get('databaseName') or '').strip()
    user = str(data.get('user') or data.get('username') or '').strip()

    if host != 'citymanager-postgis':
        continue
    if database and database != db_name:
        continue
    if user and user != db_user:
        continue

    matches.append(str(data.get('password') or ''))

if len(matches) != 1:
    raise SystemExit(
        f'Expected one matching City Manager n8n Postgres credential, got {len(matches)}'
    )
if matches[0] != expected_password:
    raise SystemExit('n8n Postgres password does not match current application password')
PY

# Prove the value shared by the application and n8n also authenticates to Postgres.
docker exec \
  -e "PGPASSWORD=$DB_PASSWORD_VALUE" \
  citymanager-postgis \
  psql \
  -h 127.0.0.1 \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  -X -qAt \
  -c 'SELECT 1;' \
  | grep -Fxq '1'

printf '%s: DATABASE CREDENTIAL ALIGNMENT PASS\n' "$LABEL"
printf 'postgis_dashboard_n8n=ALIGNED\n'
printf 'postgres_password_auth=PASS\n'
printf 'password_value=NOT_PRINTED\n'
