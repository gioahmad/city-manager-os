# PostgreSQL + PostGIS — Beginner Deployment Guide

This folder contains the first City Manager OS database deployment.

## What this gives you

After setup, the VPS will have a local PostgreSQL database with PostGIS enabled and the first City Manager OS tables created automatically.

The database is **not exposed directly to the public internet**. n8n should connect to it through the shared Docker network.

## Before you start

This guide assumes:

- Docker is already installed on the VPS.
- Docker Compose is available as `docker compose`.
- n8n is also running with Docker on the same VPS.
- You have SSH access to the VPS.

If n8n is not on the same Docker host, stop and redesign networking before exposing PostgreSQL publicly.

---

# Step 1 — Go to the server

SSH into your VPS.

Then choose a folder for the project:

```bash
cd /opt
sudo git clone https://github.com/gioahmad/city-manager-os.git
sudo chown -R "$USER":"$USER" /opt/city-manager-os
cd /opt/city-manager-os/deploy/postgis
```

If the repo is private, cloning will require GitHub authentication. An alternative is to create the folder/files manually or use an authenticated Git method.

---

# Step 2 — Create the shared Docker network

Run once:

```bash
docker network create citymanager
```

If Docker says the network already exists, that is fine.

---

# Step 3 — Create your private environment file

From `deploy/postgis`:

```bash
cp .env.example .env
nano .env
```

Change:

```text
POSTGRES_PASSWORD=CHANGE_THIS_TO_A_LONG_RANDOM_PASSWORD
```

to a long random password.

Save the file.

**Do not commit `.env` to GitHub.**

---

# Step 4 — Start PostgreSQL/PostGIS

Run:

```bash
docker compose up -d
```

Then check:

```bash
docker compose ps
```

You want `citymanager-postgis` to become healthy.

To watch startup logs:

```bash
docker compose logs -f postgis
```

Use `Ctrl+C` to leave the log view. This does not stop the database.

---

# Step 5 — Verify PostGIS

Run:

```bash
docker exec -it citymanager-postgis \
  psql -U citymanager_app -d citymanager \
  -c "SELECT PostGIS_Full_Version();"
```

You should get a PostGIS version response.

Then check the City Manager OS tables:

```bash
docker exec -it citymanager-postgis \
  psql -U citymanager_app -d citymanager \
  -c "\dt"
```

You should see tables including:

```text
watch_items
subscribers
watch_item_recipients
alerts
alert_watch_matches
deliveries
source_health
issues
gis_dataset_versions
```

---

# Step 6 — Put n8n on the same Docker network

First see the n8n container name:

```bash
docker ps
```

If the container is named `n8n`, run:

```bash
docker network connect citymanager n8n
```

Replace `n8n` with the actual container name if different.

This survives while the existing container exists, but the better long-term configuration is to add the external `citymanager` network to the n8n Docker Compose file so it is automatically reattached whenever n8n is recreated.

---

# Step 7 — Create the PostgreSQL credential in n8n

In n8n:

1. Open **Credentials**.
2. Create a new **Postgres** credential.
3. Use:

```text
Host: citymanager-postgis
Port: 5432
Database: citymanager
User: citymanager_app
Password: the password from your .env file
SSL: Disable for this local Docker-network connection
```

Save the credential.

Do not put this password in an n8n Code node or in GitHub.

---

# Step 8 — Test n8n → Postgres

Create a temporary workflow with a **Postgres** node and execute this query:

```sql
SELECT
  current_database() AS database,
  now() AS server_time,
  PostGIS_Version() AS postgis_version;
```

If it returns one row, n8n is successfully connected to the City Manager OS database.

You can delete the temporary workflow later.

---

# Useful Commands

## Check status

```bash
cd /opt/city-manager-os/deploy/postgis
docker compose ps
```

## View logs

```bash
docker compose logs --tail=100 postgis
```

## Restart database

```bash
docker compose restart postgis
```

## Stop containers without deleting data

```bash
docker compose down
```

The named Docker volume remains unless you explicitly delete it.

## Enter PostgreSQL shell

```bash
docker exec -it citymanager-postgis \
  psql -U citymanager_app -d citymanager
```

Exit with:

```text
\q
```

---

# Important: Initialization SQL runs only on a fresh database

Files in `init/` are automatically executed when the PostgreSQL data volume is initialized for the first time.

After the database already contains data, changing `001_init.sql` does **not** automatically rerun it.

Future database changes should use migration SQL files/scripts rather than deleting the database volume.

Never delete the production volume simply to apply a schema change.

---

# Backups

A basic backup script is included at:

```text
backup.sh
```

Run:

```bash
chmod +x backup.sh
./backup.sh
```

Backups are written to:

```text
./backups/
```

A later issue should automate backups and copy them to storage outside this VPS.

---

# Issue #3 Definition of Done

Issue #3 should only be closed after all of these are true:

- [ ] `citymanager-postgis` is running and healthy
- [ ] PostGIS version query succeeds
- [ ] Initial City Manager OS tables exist
- [ ] n8n is attached to the `citymanager` Docker network
- [ ] n8n Postgres credential is created
- [ ] n8n test query succeeds
- [ ] One manual database backup succeeds

Once those checks pass, the database foundation is live and Issue #3 can be closed.
