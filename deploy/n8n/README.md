# n8n Deployment Reference

This directory is the canonical City Manager OS reference for the n8n container configuration.

## Live deployment

The current production n8n stack is managed by Portainer. This repository copy is the source-of-truth reference and disaster-recovery definition; do not run `docker compose up` from this directory against the live VPS unless intentionally migrating the Portainer-managed stack.

## Required behavior

- Persist n8n state in the existing Docker volume `n8n_n8n_data`.
- Attach n8n to its default Compose network and the external `citymanager` network so workflows can reach PostGIS and other City Manager OS services by Docker DNS.
- Keep host-specific addresses and URLs in a local `.env`, not in GitHub.
- Bind port 5678 only to the intended private/local interface.

## Setup for a new host

1. Copy `.env.example` to `.env`.
2. Set the host-specific bind IP, n8n hostname/webhook URL, and ntfy host mapping.
3. Ensure the external Docker network exists:
   `docker network inspect citymanager`
4. Confirm or restore the persistent volume `n8n_n8n_data` before starting n8n.
5. Validate with `docker compose config` before deployment.

## Current production note

The Portainer-managed live stack has already been updated to include `citymanager` as an external network and was validated without restarting n8n.
