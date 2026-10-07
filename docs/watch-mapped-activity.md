# Watches and mapped activity

Every Watch builder and editor offers **Any mapped activity in this area** or
**Only activity matching filters**. All activity clears topic/aliases, source,
category, and minimum-priority restrictions. Editing keeps the selected location,
radius, recipients, and current on/paused state. A location is required.

Operational activity includes incidents, open work items, active Event Intelligence
records at any impact level, managed events, mapped transit WATCH/ALERT observations,
and current live transit vehicles. Parcel/address/reference layers and drawings are
not activity. County/town rules still use the record's labeled geography; exact
points and saved boundaries use PostGIS distance/intersection.

The existing one-minute resolved-alert workflow also routes new or changed mapped
operational records into the existing central matcher, Match audit, Subscribers,
Delivery Guard, and ntfy path. Event/Transit records reuse their canonical alert IDs.
Mapped activity is evaluated only against area rules; it cannot trigger an unrelated
source-only or topic-only Watch. Selected filters and schedules still apply.

The activation cutoff prevents replaying the historical catalog on installation.
Unchanged records are marked complete after matching. Failed deliveries remain
eligible for the existing guard's retry; pending deliveries remain suppressed by
that guard. Meaningful title/message/priority changes can notify again; coordinate
movement with identical delivery content does not cause a stream of duplicate
notifications. A mapped point just outside the radius cannot match through text.

Alerts originally received earlier can be reconsidered when the resolver maps them
after activation. Geometry changes cause reevaluation; an unchanged point is not
rematched on every poll. Ordinary live alert ingestion remains in place.

Event Intelligence and Transit cards now open a read-only monitoring draft directly
from the selected record, including records which have never emitted an Alert.
An exact point is copied when available; an unmapped venue/route falls back to the
municipality rather than being treated as an address. Map drafts open the builder.
Choosing anything near an Alert clears suggested keywords. Preview History keeps
the selected source/category, match mode/field, and minimum priority.

Deploy the dashboard, then run `install_spatial_watch_matcher.sh` and
`install_resolved_alert_rematch.sh` with the reviewed commit SHA. Those installers
retain recovery copies of the n8n database/workflows and verify publication. There
is no PostGIS schema change in this release. The full CI workflow also executes
`ci/check_watch_activity.py` against real PostgreSQL/PostGIS and tracked n8n code;
it never sends a notification or accesses production.
