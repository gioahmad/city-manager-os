# City Manager OS Unified Interface

This Phase 2 batch standardizes the visual language and timezone behavior across the City Manager OS web surfaces.

## Interface goals

### Desktop / web
- high-contrast executive presentation
- clear information hierarchy
- denser use of wide screens without crowding
- consistent cards, metrics, forms, tables and status treatments
- primary navigation kept short, with administration tools grouped separately
- Operations Board optimized for two-column ticket review on wide screens

### Mobile
- fixed bottom navigation for the private City Manager OS
- single-column operational cards and forms
- horizontally scrollable work-state tabs where appropriate
- minimum 44-54px touch targets on action controls
- employee workflows remain photo/form first and easy to operate in the field
- safe-area padding for modern phones

## Timezone

Both `citymanager-dashboard` and `citymanager-staff` run with:

```text
TZ=America/New_York
PGTZ=America/New_York
```

The database continues to store timestamps normally; timezone is applied at the application/session display layer. This keeps the private dashboard, alerts, operations work, activity history and employee portal aligned to Eastern Time, including DST.

## Architecture

No operational tables, n8n workflows, routing, alert ingestion or raw feeds are changed by this batch.

The private UI continues to use:
- `static/style.css` for shared foundation
- `static/ops.css` for private navigation and operations components

The public employee UI continues to use:
- `static/staff.css`

All three now share the same typography, navy/blue civic palette, spacing, border radii, status semantics and responsive behavior.
