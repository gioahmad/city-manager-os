# Decision Log

## D001 - Central routing
**Decision:** Do not maintain recipient lists separately inside every automation.

**Why:** Per-source subscriber lists duplicate configuration and become difficult to keep synchronized.

**Status:** Accepted

---

## D002 - Watchlist-first model
**Decision:** Maintain important addresses, facilities, areas, phrases, sources and conditions once in a Master Watchlist.

**Why:** The operational question is "what do we care about, and who should know when it appears?" rather than "what does each user individually subscribe to?"

**Status:** Accepted

---

## D003 - Standard Alert Schema
**Decision:** Every source should output the same normalized alert object before entering the central router when the source represents alertable operational intelligence.

**Why:** Source-specific parsing should remain in source workflows; routing logic should not understand every upstream API.

**Status:** Accepted

---

## D004 - Local GIS
**Decision:** Static/slow-changing GIS should be downloaded and stored locally in PostGIS instead of queried live during incidents.

**Why:** Faster local queries, fewer runtime dependencies and resilience when upstream GIS services are unavailable.

**Status:** Accepted

---

## D005 - n8n is the automation engine, not the interface
**Decision:** Normal monitoring and data management should happen through City Manager OS rather than inside the n8n editor whenever practical.

**Why:** n8n is excellent for automation but is not the municipal operating interface.

**Status:** Accepted

---

## D006 - ntfy is the interruption layer
**Decision:** The dashboard shows the full picture; ntfy should interrupt only when watchlist/rules determine something deserves attention.

**Status:** Accepted

---

## D007 - Separate operational data from project documentation
**Decision:** GitHub contains architecture, sanitized templates, schemas and workflow exports; confidential/live operational data remains on controlled infrastructure.

**Status:** Accepted

---

## D008 - Start with one end-to-end source
**Decision:** Prove the complete architecture with PSEG before migrating every source.

**Why:** A working end-to-end pattern is more valuable than partially converting many feeds at once.

**Status:** Accepted / completed

---

## D009 - Command Center remains the operational source of truth
**Decision:** The existing `issues` table / Command Center remains the single operational issue/task source of truth.

**Why:** Events, commitments, integrations and recurring operations should not create parallel task databases that fragment accountability.

**Status:** Accepted

---

## D010 - Web administration is the next control plane
**Decision:** Frequently changed municipal operating configuration should increasingly be managed through authenticated City Manager OS web pages instead of CLI, SQL or direct n8n editing.

**Initial scope:** Watchlists, subscribers, routing, events, integrations, routines, source health and appropriate rule configuration.

**Status:** Accepted

---

## D011 - Integrations Center pattern
**Decision:** New external APIs should be administered through a common Integrations Center pattern rather than each source inventing its own management experience.

**Why:** The Township Manager should be able to see what sources exist, whether they are enabled and healthy, when they last succeeded, and how they feed the OS from one place.

**Security:** Credentials remain protected server-side and are not committed to GitHub or fully exposed in the UI.

**Status:** Accepted

---

## D012 - Awareness is not automatically action
**Decision:** External signals, routine status and events do not automatically become Command Center issues.

**Rule:** Create accountable work when an action is required, someone can own it, completion can be verified and delay matters.

**Why:** Turning all intelligence into issues creates noise and destroys accountability.

**Status:** Accepted

---

## D013 - Events are first-class context, not a second task system
**Decision:** Add an Events Center for scheduled municipal activity, preparation and follow-up, while using the existing Command Center for actionable work.

**Status:** Accepted

---

## D014 - NJ Transit follows the common intelligence pipeline
**Decision:** NJ Transit integration should use the Integrations Center, source health, Standard Alert Schema where appropriate, existing Watchlist / Rules, dynamic recipient routing and Delivery Guard.

**Why:** Transit should become another intelligence source inside one municipal OS, not a disconnected app.

**Status:** Accepted

---

## D015 - Obsidian is deferred and will be a knowledge source
**Decision:** Defer Obsidian/local-document integration until the current operational and API-administration priorities are stable.

**Future role:** Knowledge/document source and search context, not a replacement for operational records.

**Status:** Accepted / deferred
