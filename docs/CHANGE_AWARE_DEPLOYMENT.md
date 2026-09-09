# Change-Aware Deployment Harness

`deploy/cmos-deploy` computes the smallest safe build, test, restart, and
acceptance plan from the Git diff between an accepted base and a target.

## Modes

```bash
./deploy/cmos-deploy plan --base origin/main --target HEAD
./deploy/cmos-deploy verify --base origin/main --target HEAD
./deploy/cmos-deploy apply --base origin/main --target HEAD
```

- `plan` is read-only and prints every decision.
- `verify` performs static validation, builds the shared image only when an
  application file changed, and runs only mapped tests. It never recreates a
  production container.
- `apply` repeats verification, recreates only affected application services,
  runs live health, and runs secure E2E only for material changes.

The working tree must be clean for `verify` and `apply`. The base must be an
ancestor of the target. Unknown paths fail closed.

## Service boundaries

- Ordinary dashboard templates, static assets, and dashboard application code:
  rebuild the image and recreate `citymanager-dashboard` only.
- Employee/public staff code and staff-specific assets: recreate Dashboard and
  Staff.
- Operations engine code: recreate Operations Engine only.
- Integration worker code: recreate Integration Engine only.
- Runtime modules shared by Dashboard and Integration Engine: recreate those
  two services only.
- Dockerfile, Python requirements, or application Compose changes: recreate all
  four application services.

All application services intentionally share the same built image. Containers
not recreated continue using their existing immutable image layer.

Before an application build in `apply` mode, the harness records the current
image ID of every affected service. Readiness requires three consecutive
successful checks; Dashboard must also return HTTP 200 from `/health`, and
Staff must accept an internal connection. If restart, health, or E2E validation
fails, the harness recreates each affected service from its recorded image and
restores the prior `latest` tag. Database and n8n rollback remain the guarded
feature installer's responsibility.

## Database, n8n, and infrastructure changes

The generic harness does not guess how to apply a database migration, publish
an n8n workflow, or alter infrastructure. These changes are marked `external`
and `apply` stops before touching production.

A guarded feature installer must:

1. run the harness in `verify` mode;
2. create and validate required backups;
3. apply the migration, workflow publication, or infrastructure change;
4. invoke `apply --external-applied` for targeted application recreation,
   health checks, and material E2E acceptance.

`--external-applied` is an explicit assertion, not an automatic installer.

## E2E policy

Secure full E2E is reserved for changes that materially cross system
boundaries, including:

- database migrations and shared schemas;
- n8n workflows;
- Staff, Operations Engine, or Integration Engine runtime behavior;
- shared application image definitions;
- authentication/security procedures.

Dashboard-only presentation and ordinary dashboard application changes use
targeted tests plus live health without forcing full E2E.

## Classification tests

```bash
./deploy/test_change_aware_deploy.sh
```

The tests are synthetic and do not build images, restart containers, publish
workflows, or touch PostgreSQL.
