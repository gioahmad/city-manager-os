# City Manager OS Phase 2 — Operations Access Hardening

This batch keeps `employee_app.py` as the existing employee workflow engine and places `employee_public_app.py` in front of it as the public entry point.

## What changes

- Employee-facing URLs no longer expose `STAFF_TOKEN`.
- Canonical employee routes are `/staff`, `/staff/home`, `/staff/report`, `/staff/work`, `/staff/ticket/...`, and `/staff/supervisor`.
- Existing tokenized employee links remain compatible and redirect to the tokenless route.
- Optional secure-cookie mode is available for HTTPS cutover.
- Optional trusted-host, public-origin, and reverse-proxy settings are available.
- Failed login attempts receive a lightweight per-client throttle.
- Employee Operations responses receive no-cache, no-referrer, frame-denial, content-type, CSP, and no-index headers.
- The employee operations container can be bound to localhost after a reverse proxy is active.
- No issue, employee, photo, checklist, or workflow schema is changed.

## Safe activation order on the VPS

1. Confirm `ops.nhnj.us` DNS points to the VPS.
2. Inspect what currently owns ports 80 and 443 before adding or changing any reverse proxy.
3. Put TLS/reverse proxy in front of `127.0.0.1:8091` and verify `https://ops.nhnj.us/health`.
4. Set the production values in `dashboard/.env`:

```dotenv
STAFF_SECURE_COOKIES=true
STAFF_TRUST_PROXY=true
STAFF_PUBLIC_ORIGIN=https://ops.nhnj.us
STAFF_ALLOWED_HOSTS=ops.nhnj.us,localhost,127.0.0.1
STAFF_BIND_IP=127.0.0.1
```

5. Rebuild the shared dashboard image and recreate only `citymanager-staff` as required by the existing image relationship.
6. Test employee sign-in, report issue, photo upload, My Work, Start Work, Need Help, Complete, supervisor queue, assignment, and private photo access.
7. Run the existing City Manager OS health check and confirm n8n still reports all active workflows accounted for.
8. Only after those checks should the old direct `:8091` path be considered retired.

## Reverse proxy target

The intended public target is:

```text
https://ops.nhnj.us  ->  http://127.0.0.1:8091
```

The public-facing product name is **Weehawken Operations**. Internal code, container names, and existing `/staff` route paths remain unchanged in this hardening batch to minimize deployment risk.

## Rollback

The application workflow engine is unchanged. Rollback is limited to switching the staff container command from:

```text
employee_public_app:staff_app
```

back to:

```text
employee_app:staff_app
```

and restoring the prior bind/settings. Existing operational data is unaffected.
