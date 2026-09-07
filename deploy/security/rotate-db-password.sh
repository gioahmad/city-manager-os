#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="/opt/city-manager-os"
POSTGIS_ENV="$ROOT/deploy/postgis/.env"
DASH_ENV="$ROOT/dashboard/.env"
STAMP="$(date +%Y%m%d_%H%M%S)"
SECURE_DIR="/var/backups/city-manager-os/security/$STAMP"
HOST_ALL="/dev/shm/cmos-n8n-credentials-all-$STAMP.json"
HOST_ORIGINAL="/dev/shm/cmos-n8n-postgres-original-$STAMP.json"
HOST_UPDATED="/dev/shm/cmos-n8n-postgres-updated-$STAMP.json"
N8N_EXPORT="/tmp/cmos-credentials-$STAMP.json"
N8N_UPDATED="/tmp/cmos-postgres-updated-$STAMP.json"
N8N_ORIGINAL="/tmp/cmos-postgres-original-$STAMP.json"
ROTATED=0
ENV_UPDATED=0
N8N_UPDATED_FLAG=0

mkdir -p "$SECURE_DIR"
chmod 700 "$SECURE_DIR"

[[ -f "$POSTGIS_ENV" ]] || { echo "ERROR: missing $POSTGIS_ENV"; exit 1; }
[[ -f "$DASH_ENV" ]] || { echo "ERROR: missing $DASH_ENV"; exit 1; }

set -a
source "$POSTGIS_ENV"
set +a

[[ "$POSTGRES_USER" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
  echo "ERROR: unsafe POSTGRES_USER"
  exit 1
}

OLD_PASSWORD="$POSTGRES_PASSWORD"
NEW_PASSWORD="$(python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
)"

N8N_DIR="$(docker inspect n8n --format '{{range .Mounts}}{{if eq .Destination "/home/node/.n8n"}}{{.Source}}{{end}}{{end}}')"
N8N_DB="$N8N_DIR/database.sqlite"
[[ -f "$N8N_DB" ]] || { echo "ERROR: n8n database.sqlite not found"; exit 1; }
N8N_UID="$(stat -c %u "$N8N_DB")"
N8N_GID="$(stat -c %g "$N8N_DB")"

cleanup_sensitive(){
  rm -f "$HOST_ALL" "$HOST_ORIGINAL" "$HOST_UPDATED"
  docker exec n8n rm -f "$N8N_EXPORT" "$N8N_UPDATED" "$N8N_ORIGINAL" >/dev/null 2>&1 || true
}

sql_password(){
  python3 - "$POSTGRES_USER" "$1" <<'PY'
import sys
role = sys.argv[1].replace('"', '""')
password = sys.argv[2].replace("'", "''")
print(f'ALTER ROLE "{role}" PASSWORD \'{password}\';')
PY
}

restore_env_files(){
  cp -f "$SECURE_DIR/postgis.env.before" "$POSTGIS_ENV"
  cp -f "$SECURE_DIR/dashboard.env.before" "$DASH_ENV"
  chmod 600 "$POSTGIS_ENV" "$DASH_ENV"
}

restart_consumers(){
  cd "$ROOT/dashboard"
  docker compose up -d --no-deps --force-recreate \
    citymanager-dashboard \
    citymanager-staff \
    citymanager-ops-engine \
    citymanager-integration-engine >/dev/null
}

prepare_n8n_import_file(){
  local path="$1"
  docker exec -u root n8n chown node:node "$path" >/dev/null 2>&1 || return 1
  docker exec -u root n8n chmod 600 "$path" >/dev/null 2>&1 || return 1
}

restore_n8n_original(){
  if [[ -s "$HOST_ORIGINAL" ]]; then
    docker cp "$HOST_ORIGINAL" "n8n:$N8N_ORIGINAL" >/dev/null 2>&1 || return 1
    prepare_n8n_import_file "$N8N_ORIGINAL" || return 1
    docker exec -u node n8n n8n import:credentials --input="$N8N_ORIGINAL" >/dev/null 2>&1 || return 1
    docker restart n8n >/dev/null 2>&1 || return 1
    return 0
  fi
  return 1
}

restore_n8n_sqlite_snapshot(){
  docker stop n8n >/dev/null 2>&1 || true
  cp -f "$SECURE_DIR/n8n-database.sqlite" "$N8N_DB"
  chown "$N8N_UID:$N8N_GID" "$N8N_DB"
  chmod 600 "$N8N_DB"
  rm -f "$N8N_DB-wal" "$N8N_DB-shm"
  docker start n8n >/dev/null 2>&1 || true
}

rollback(){
  local rc=$?
  set +e

  if (( ROTATED == 1 )); then
    sql_password "$OLD_PASSWORD" | docker exec -i citymanager-postgis \
      psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 || true
  fi

  if (( ENV_UPDATED == 1 )); then
    restore_env_files || true
  fi

  if (( N8N_UPDATED_FLAG == 1 )); then
    restore_n8n_original || restore_n8n_sqlite_snapshot || true
  fi

  restart_consumers || true
  cleanup_sensitive

  echo "DATABASE CREDENTIAL ROTATION: FAIL rc=$rc"
  echo "Rollback attempted; inspect health before retrying."
  exit "$rc"
}
trap rollback ERR
trap cleanup_sensitive EXIT

# Verify the installed n8n version supports the supported export/import path
# before making any credential or database change.
docker exec -u node n8n n8n export:credentials --help 2>&1 | grep -q -- '--decrypted'
docker exec -u node n8n n8n import:credentials --help 2>&1 | grep -q -- '--input'

printf 'Creating pre-rotation PostgreSQL backup...\n'
"$ROOT/deploy/postgis/backup.sh" >/dev/null
"$ROOT/deploy/postgis/verify-backup.sh" >/dev/null

printf 'Creating n8n SQLite online backup...\n'
python3 - "$N8N_DB" "$SECURE_DIR/n8n-database.sqlite" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
chmod 600 "$SECURE_DIR/n8n-database.sqlite"

cp "$POSTGIS_ENV" "$SECURE_DIR/postgis.env.before"
cp "$DASH_ENV" "$SECURE_DIR/dashboard.env.before"
chmod 600 "$SECURE_DIR/postgis.env.before" "$SECURE_DIR/dashboard.env.before"

printf 'Exporting n8n credentials for controlled update...\n'
docker exec -u node n8n n8n export:credentials --all --decrypted --output="$N8N_EXPORT" >/dev/null
docker cp "n8n:$N8N_EXPORT" "$HOST_ALL" >/dev/null
chmod 600 "$HOST_ALL"

python3 - "$HOST_ALL" "$HOST_ORIGINAL" "$HOST_UPDATED" "$NEW_PASSWORD" "$POSTGRES_USER" "$POSTGRES_DB" <<'PY'
import json, sys
from pathlib import Path

source, original_out, updated_out, new_password, db_user, db_name = sys.argv[1:]
credentials = json.loads(Path(source).read_text())
if not isinstance(credentials, list):
    raise SystemExit('n8n credential export is not a list')

original = []
updated = []
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

    original.append(item)
    changed = json.loads(json.dumps(item))
    changed['data']['password'] = new_password
    updated.append(changed)

if not updated:
    raise SystemExit('No n8n Postgres credential matched citymanager-postgis')

Path(original_out).write_text(json.dumps(original))
Path(updated_out).write_text(json.dumps(updated))
print(f'n8n_matching_postgres_credentials={len(updated)}')
PY
chmod 600 "$HOST_ORIGINAL" "$HOST_UPDATED"

docker cp "$HOST_ORIGINAL" "n8n:$N8N_ORIGINAL" >/dev/null
docker cp "$HOST_UPDATED" "n8n:$N8N_UPDATED" >/dev/null
prepare_n8n_import_file "$N8N_ORIGINAL"
prepare_n8n_import_file "$N8N_UPDATED"

printf 'Rotating PostgreSQL role password...\n'
sql_password "$NEW_PASSWORD" | docker exec -i citymanager-postgis \
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null
ROTATED=1

python3 - "$POSTGIS_ENV" "$DASH_ENV" "$NEW_PASSWORD" <<'PY'
from pathlib import Path
import os
import sys

postgis = Path(sys.argv[1])
dashboard = Path(sys.argv[2])
password = sys.argv[3]


def replace(path: Path, key: str, value: str) -> None:
    lines = path.read_text().splitlines()
    found = False
    out = []
    for line in lines:
        if line.startswith(key + '='):
            out.append(f'{key}={value}')
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f'{key}={value}')
    path.write_text('\n'.join(out).rstrip() + '\n')
    os.chmod(path, 0o600)


replace(postgis, 'POSTGRES_PASSWORD', password)
replace(dashboard, 'DB_PASSWORD', password)
PY
ENV_UPDATED=1

printf 'Updating n8n Postgres credential...\n'
N8N_UPDATED_FLAG=1
docker exec -u node n8n n8n import:credentials --input="$N8N_UPDATED" >/dev/null
docker restart n8n >/dev/null

printf 'Restarting City Manager OS database consumers...\n'
restart_consumers

for _ in $(seq 1 45); do
  if curl -fsS http://100.94.203.47:8090/health >/dev/null 2>&1 \
     && curl -fsS http://127.0.0.1:8091/health >/dev/null 2>&1 \
     && docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

curl -fsS http://100.94.203.47:8090/health >/dev/null
curl -fsS http://127.0.0.1:8091/health >/dev/null
docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))" >/dev/null

for c in n8n citymanager-dashboard citymanager-staff citymanager-ops-engine citymanager-integration-engine; do
  [[ "$(docker inspect "$c" --format '{{.State.Status}}')" == "running" ]]
done

# Old env snapshots contain unrelated local secrets and are no longer required
# after a successful rotation. Keep the encrypted n8n SQLite snapshot only.
rm -f "$SECURE_DIR/postgis.env.before" "$SECURE_DIR/dashboard.env.before"

cleanup_sensitive
trap - ERR

printf 'DATABASE CREDENTIAL ROTATION: PASS\n'
printf 'n8n_credentials_updated=YES\n'
printf 'dashboard_env_updated=YES\n'
printf 'postgis_env_updated=YES\n'
printf 'n8n_pre_rotation_snapshot=%s\n' "$SECURE_DIR/n8n-database.sqlite"
printf 'password_value=NOT_PRINTED\n'
