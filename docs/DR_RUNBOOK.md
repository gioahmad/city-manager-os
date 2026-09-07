# City Manager OS Disaster Recovery Runbook

## Purpose
Restore City Manager OS after VPS loss, database corruption, accidental destructive change, or a failed infrastructure migration.

This runbook covers the recovery order for:
1. host/network prerequisites
2. local secrets/environment configuration
3. PostgreSQL/PostGIS
4. n8n automation state
5. City Manager OS application containers
6. health and controlled E2E acceptance

Do not restore by guessing the order. PostgreSQL and n8n are stateful dependencies and should be recovered before application acceptance.

---

## Recovery assets that must exist

### Repository
- `gioahmad/city-manager-os`
- restore the production `main` commit intended for recovery

### PostgreSQL backup
- `deploy/postgis/backups/citymanager_YYYYMMDD_HHMMSS.dump`
- matching `.sha256`
- preferably an off-box copy

### Local environment files
These are intentionally not stored in GitHub:
- `/opt/city-manager-os/deploy/postgis/.env`
- `/opt/city-manager-os/dashboard/.env`
- n8n deployment environment including its encryption key, if explicitly configured

### n8n persistent state
- the n8n persistent `.n8n` directory or a consistent `database.sqlite` backup
- n8n encryption-key configuration must match the state being restored

### Persistent application data
- `/var/lib/city-manager-os/staff_uploads`
- `/var/lib/city-manager-os/transit_tokens` is cacheable and may be regenerated, but restoring it avoids unnecessary token churn

---

# Recovery order

## 1. Prepare the replacement host

Install/verify:
- Docker Engine
- Docker Compose
- Git
- rsync if off-box backups use rsync
- Tailscale or the private network used for administrative access

Create the shared Docker network if missing:

```bash
docker network inspect citymanager >/dev/null 2>&1 || docker network create citymanager
```

Restore the repository:

```bash
mkdir -p /opt
cd /opt
git clone https://github.com/gioahmad/city-manager-os.git
cd /opt/city-manager-os
git switch main
git pull --ff-only origin main
```

## 2. Restore local environment configuration

Restore the protected `.env` files before starting dependent services:

```text
/opt/city-manager-os/deploy/postgis/.env
/opt/city-manager-os/dashboard/.env
```

Permissions:

```bash
chmod 600 \
  /opt/city-manager-os/deploy/postgis/.env \
  /opt/city-manager-os/dashboard/.env
```

Never copy secrets into GitHub to make recovery easier.

## 3. Start empty PostgreSQL/PostGIS

From:

```bash
cd /opt/city-manager-os/deploy/postgis
```

Start PostGIS:

```bash
docker compose up -d
```

Wait for healthy state:

```bash
docker inspect citymanager-postgis --format '{{.State.Health.Status}}'
```

## 4. Validate the selected PostgreSQL backup

Before restoring:

```bash
cd /opt/city-manager-os/deploy/postgis
sha256sum -c backups/citymanager_YYYYMMDD_HHMMSS.dump.sha256

docker exec -i citymanager-postgis \
  pg_restore --list \
  < backups/citymanager_YYYYMMDD_HHMMSS.dump \
  >/dev/null
```

If either validation fails, do not restore that archive. Use the previous validated copy.

## 5. Restore PostgreSQL/PostGIS

For a completely empty replacement database, restore the archive with owner/privilege information appropriate to the recovered environment.

The controlled scratch drill uses `--no-owner --no-privileges`; a full production recovery may preserve the original ownership when the original role exists.

Example for the standard City Manager OS role:

```bash
set -a
source /opt/city-manager-os/deploy/postgis/.env
set +a

docker exec -i citymanager-postgis \
  pg_restore \
  --exit-on-error \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  < /path/to/citymanager_YYYYMMDD_HHMMSS.dump
```

Verify core tables:

```bash
docker exec citymanager-postgis \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT to_regclass('public.issues'),to_regclass('public.alerts'),to_regclass('public.integrations'),to_regclass('public.map_layers');"
```

## 6. Restore n8n state

Do this before relying on ingestion/routing.

Restore the n8n persistent directory or `database.sqlite` backup while n8n is stopped.

Example pattern:

```bash
docker stop n8n
# restore the persistent .n8n content/database.sqlite here
# preserve the original UID/GID ownership used by the n8n container
docker start n8n
```

The n8n encryption key must be the same key used to encrypt the restored credentials. If the key is lost, encrypted credentials cannot simply be reconstructed from the SQLite database.

Check:

```bash
docker exec n8n node -e "fetch('http://127.0.0.1:5678/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
```

Do not publish/deactivate workflows merely as part of recovery unless the recovered state requires it.

## 7. Restore persistent uploaded files

Restore if available:

```text
/var/lib/city-manager-os/staff_uploads
```

Ensure the dashboard/staff containers can read the restored files.

Transit tokens may be restored or regenerated:

```text
/var/lib/city-manager-os/transit_tokens
```

## 8. Start City Manager OS application containers

```bash
cd /opt/city-manager-os/dashboard

docker compose build citymanager-dashboard

docker compose up -d \
  citymanager-dashboard \
  citymanager-staff \
  citymanager-ops-engine \
  citymanager-integration-engine
```

## 9. Verify private authentication

The private dashboard should allow unauthenticated `/health` but redirect protected browser pages such as `/my-day` to `/login`.

The employee portal remains independently authenticated at its existing `/staff` flow.

## 10. Health acceptance

```bash
cd /opt/city-manager-os
./deploy/cmos-health
```

Do not proceed if required containers, routing, or source state is unexpectedly unhealthy.

## 11. Controlled E2E acceptance

When private dashboard authentication is enabled use:

```bash
cd /opt/city-manager-os
./deploy/cmos-e2e-secure
```

The automation token is read from the protected local dashboard `.env` and is not printed.

Acceptance target:
- 24 checks pass
- synthetic records cleaned
- E2E n8n harness returned inactive
- post-test health clean

---

# Routine backup verification

Nightly backup timer:

```bash
systemctl status citymanager-postgis-backup.timer
```

Manual backup:

```bash
/opt/city-manager-os/deploy/postgis/backup.sh
```

Manual verification:

```bash
/opt/city-manager-os/deploy/postgis/verify-backup.sh
```

Controlled scratch restore drill:

```bash
/opt/city-manager-os/deploy/postgis/scratch-restore.sh
```

The scratch drill creates a temporary database, restores the latest archive, verifies key tables, then drops the scratch database.

---

# Off-box backups

Configure one of these only in `deploy/postgis/.env`:

```text
BACKUP_OFFBOX_DIR=/mounted/remote/storage/city-manager-os
```

or:

```text
BACKUP_OFFBOX_TARGET=backupuser@remote-host:/srv/backups/city-manager-os
```

Once a real off-VPS target is operational set:

```text
BACKUP_REQUIRE_OFFBOX=true
```

At that point a nightly backup fails loudly if it cannot create the off-box copy.

A local directory on the same production VPS does **not** satisfy the off-box requirement.

---

# Credential rotation

Use the coordinated operator command:

```bash
/opt/city-manager-os/deploy/security/rotate-db-password.sh
```

It backs up first, updates the PostgreSQL role, dashboard/engine env, and matching n8n Postgres credential, then restarts consumers. The generated password is never printed.

Private dashboard first-login setup:

```bash
/opt/city-manager-os/deploy/security/bootstrap-private-auth.sh
```

The initial Executive credential is written to a root-only local file and should be moved into the approved password manager, then deleted.

---

# Recovery rule

After any real disaster recovery, do not declare City Manager OS restored until:
1. PostgreSQL backup validates
2. n8n state/credentials load
3. private dashboard authentication works
4. employee portal works
5. `cmos-health` is clean
6. `cmos-e2e-secure` passes
7. synthetic test data is cleaned
