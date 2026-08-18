# Master Watchlist

## Purpose

The Master Watchlist is the central list of things City Manager OS cares about. Each item exists once and can assign one or more recipients.

## Watch Types

Recommended initial types:

- ADDRESS
- FACILITY
- PLACE
- AREA
- TOWN
- COUNTY
- PERSON
- KEYWORD
- PHRASE
- SOURCE
- CATEGORY
- INCIDENT_TYPE

## Core Fields

```text
watch_id
active
watch_type
search_term
display_name
aliases
match_mode
category
subcategory
address
municipality
county
state
zip
block
lot
qualifier
parcel_id
latitude
longitude
radius_ft
nearby_enabled
gis_enabled
notes
```

Recipient columns may initially be simple Boolean fields such as:

```text
gio
mayor
police
fire
dpw
oem
```

If the recipient population grows significantly, normalize recipient assignments into a separate table.

## Match Modes

- `CONTAINS` — search term appears anywhere in searchable text
- `WORD` — whole-word/phrase matching
- `EXACT` — exact normalized match
- `FIELD` — compare against a structured alert field such as municipality/source/category

## Aliases

Use pipe-separated aliases in the CSV template.

Example:

```text
4100 PARK AVE|4100 PARK AVENUE|4100 PARK
```

Avoid aliases so broad that they generate unrelated matches.

## Example Flow

```text
Incoming Alert
     ↓
Normalize searchable text/fields
     ↓
Evaluate active watch rows
     ↓
Matched watch rows
     ↓
Collect checked recipients
     ↓
Deduplicate recipients
     ↓
Subscriber Directory
     ↓
ntfy
```

## Future GIS Behavior

A matched address/facility can be enriched with:

- Block/Lot
- Parcel ID
- Coordinates
- Property attributes
- Nearby parcels
- Nearby watchlist locations
- Flood zone membership
- Other GIS layers

The watchlist should store identifiers and metadata, not duplicate entire external GIS datasets.
