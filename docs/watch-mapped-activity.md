# Watches and mapped activity

Every Watch builder and editor offers **Any mapped activity in this area** or
**Only activity matching filters**. All activity clears topic/aliases, source,
category, and minimum-priority restrictions. Editing keeps the selected location,
radius, recipients, and current on/paused state. A location is required.

Filtered Watches accept up to 200 editable words/phrases, separated by lines,
pipes, or commas. Any one term matches; case is ignored. Choose Contains any term
or Whole words or phrases only. `\d` is a literal-term digit placeholder, not an
unrestricted regular expression. Source, location/distance, priority/category,
recipients, duration and on/paused state remain selectable. Saved terms all appear
in the editor, and history preview retains the full list instead of stopping at 12.
No BNN keyword list or recipient is embedded in application code.

**Sources** uses the same checkbox dropdown in Create and Edit. Select any number
of sources, or choose **All sources** to remove the source restriction, including
for sources added later. The dropdown retains saved or prefilled source choices.

Saved Watch cards always offer **Edit**, **Matches**, **Preview History**, and
**Manage**. **On** means the Watch is enabled for current activity; the separate
**With matches · 7 days** count shows Watches with actual recent matches. Cards
show **Scheduled**, **Notification pending**, or **Repeat held** when applicable.
Matches shows recorded matches; Preview History tests the current rule against
stored alerts even if those alerts predate the Watch.

**Notification options** on Watches offers an Executive-controlled global switch
for “Why you received this.” The central ntfy sender reads the saved setting for
each delivery, covering every source and recipient without republishing workflows.
It defaults on and changes no matching, routing, original alert body, or audit data.

Operational activity includes incidents, open work items, active Event Intelligence
records at any impact level, managed events, mapped transit WATCH/ALERT observations,
and current live transit vehicles. Parcel/address/reference layers and drawings are
not activity. County/town rules still use the record's labeled geography; exact
points and saved boundaries use PostGIS distance/intersection.

**Preview History** is available for every saved Watch, including radius, entity,
and adjoining-parcel areas. It keeps the complete saved rule: all sources and
categories, priority, words/alternatives, and the saved geometry. The preview starts
with **All stored history** and allows shorter periods. It tests the current rule
even when the Watch is paused or expired, independently of its start/end dates and
whether an alert ever produced a Match. Stored alert geometry takes precedence;
a resolved location is used when the stored point is absent. Unresolved/unmapped
alerts cannot be assigned to a radius.

Click a blank spot on the Map, use **Pick Watch Point / Search History**, or choose
**Preview History Here** on a mapped feature. Set a radius in feet (1–26,400); the
circle redraws as the radius changes. **Preview History Here** searches all stored
alerts in that radius without creating a Watch. The history page can adjust the
radius and period, or open an optional Watch draft with the same point and radius.
Both entry points count every eligible alert and show 100 matches per page, newest
first. Previous/Next controls reach older results; there is no 25,000-candidate
cutoff. A server cursor streams the query results and keeps only the current page.
Previews never write Watches, Matches, deliveries, or Notifications.

History includes mapped operational activity already saved in the alert catalog.
It does not reconstruct past vehicle positions or operational records that have
never been saved as alerts. Reference layers and drawings are not alert history.

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
`install_resolved_alert_rematch.sh`, then `install_ntfy_match_explanations.sh` with
the reviewed commit SHA. Those installers
retain recovery copies of the n8n database/workflows and verify publication. There
is no PostGIS schema change in this release. The full CI workflow also executes
`ci/check_watch_activity.py` against real PostgreSQL/PostGIS and tracked n8n code;
it never sends a notification or accesses production.

