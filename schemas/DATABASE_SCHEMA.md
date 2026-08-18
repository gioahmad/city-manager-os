# Database Schema — Initial Direction

## Purpose

Define the first PostgreSQL/PostGIS tables required to move City Manager OS from scattered n8n state into a shared operational data model.

## Core Tables

### `watch_items`

Suggested fields:

```text
id                 UUID / serial
watch_id           text unique
active             boolean
watch_type         text
search_term        text
display_name       text
aliases            text[] or jsonb
match_mode         text
category           text
subcategory        text
address            text
municipality       text
county             text
state              text
zip                 text
block               text
lot                 text
qualifier           text
parcel_id           text
latitude            numeric
longitude           numeric
radius_ft           numeric
nearby_enabled      boolean
gis_enabled         boolean
gis_lookup          text
source_notes        text
notes               text
min_priority        integer
geom                geometry(Point, appropriate SRID) where applicable
created_at          timestamptz
updated_at          timestamptz
```

### `subscribers`

```text
id
subscriber_id       text unique
name                text
active              boolean
ntfy_topic          text
notes               text
created_at
updated_at
```

### `watch_item_recipients`

Use this normalized table when moving beyond simple CSV checkbox columns.

```text
id
watch_item_id
subscriber_id
active
```

### `alerts`

```text
id
alert_id             text unique
source               text
category             text
subtype              text
county               text
municipality         text
title                text
message              text
priority             integer
tags                 jsonb
click_url             text
source_timestamp      timestamptz
received_at           timestamptz
raw_payload           jsonb
search_text           text
geom                  geometry(Point, appropriate SRID)
```

### `alert_watch_matches`

```text
id
alert_id
watch_item_id
match_type
match_reason
matched_at
```

### `deliveries`

```text
id
delivery_key          text unique
alert_id
subscriber_id
ntfy_topic
status
attempted_at
sent_at
error_message
matched_watch_ids      jsonb
```

### `source_health`

```text
source_id              text unique
status                 text
last_attempt_at        timestamptz
last_success_at        timestamptz
last_event_at          timestamptz
last_error             text
metadata               jsonb
```

### `issues`

Future dashboard/manual intake table:

```text
id
title
description
category
priority
status
source
address
municipality
assigned_to
watch_item_id
created_at
updated_at
closed_at
geom
```

## GIS Tables

Keep external GIS data separate from operational tables.

Initial examples:

```text
gis_parcels
gis_addresses
gis_municipalities
gis_flood_zones
```

Use staging tables during dataset refreshes so failed imports do not replace working production GIS data.

## Design Rule

The watchlist references GIS identifiers/geometry where useful, but does not duplicate the complete parcel/flood/address datasets.
