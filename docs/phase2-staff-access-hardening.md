# City Manager OS Phase 2 — Staff Access Hardening

This batch keeps `employee_app.py` as the existing employee workflow engine and places `employee_public_app.py` in front of it as the public entry point.

## What changes

- Employee-facing URLs no longer expose `STAFF_TOKEN`.
- Canonical employee routes are `/staff`, `/staff/home`, `/staff/report`, `/staff/work`, `/staff/ticket/...`, and `/staff/supervisor`.
- Existing tokenized employee links remain compatible and redirect to the tokenless route.
- Optional secure-cookie mode is available for HTTPS cutover.
- Optional trusted-host, public-origin, and reverse-proxy settings are available.
- Failed login attempts receive a lightweight per-client throttle.
- Staff responses receive no-cache, no-referrer, frame-denial, content-type, CSP, and no-index headers.
- The staff container can be bound to localhost after a reverse proxy is active.
- No issue, employee, photo, checklist, or workflow schema is changed.

## Safe activation order on the VPS

1. Confirm `staff.nhnj.us` DNS points to the VPS.
2. Inspect what currently owns ports 80 and 443 before adding or changing any reverse proxy.
3. Put TLS/reverse proxy in front of `127.0.0.1:8091` and verify `https://staff.nhnj.us/health`.
4. Set the staff production values in `dashboard/.env`:

```dotenv
STAFF_SECURE_COOKIES=true
STAFF_TRUST_PROXY=true
STAFF_PUBLIC_ORIGIN=https://staff.nhnj.us
STAFF_ALLOWED_HOSTS=staff.nhnj.us,localhost,127.0.0.1
STAFF_BIND_IP=127.0.0.1
```

5. Rebuild/recreate only `citymanager-dashboard` and `citymanager-staff` as required by the existing image relationship.
6. Test employee sign-in, report issue, photo upload, My Work, Start Work, Need Help, Complete, supervisor queue, assignment, and private photo access.
7. Run the existing City Manager OS health check and confirm n8n still reports all active workflows accounted for.
8. Only after those checks should the old direct `:8091` path be considered retired.

## Reverse proxy target

The intended public target is:

```text
https://staff.nhnj.us  ->  http://127.0.0.1:8091
```

Do not install a second reverse proxy blindly. Inspect the VPS first because another service may already own ports 80/443.

## Rollback

The application workflow engine is unchanged. Rollback is limited to switching the staff container command from:

```text
employee_public_app:staff_app
```

back to:

```text
employee_app:staff_app
```

and restoring the prior staff bind/settings. Existing operational data is unaffected.
