# Decision Log

## D001 — Central routing
**Decision:** Do not maintain recipient lists separately inside every automation.

**Why:** Per-source subscriber lists duplicate configuration and become difficult to keep synchronized.

**Status:** Accepted

---

## D002 — Watchlist-first model
**Decision:** Maintain important addresses, facilities, areas, phrases, sources, and conditions once in a Master Watchlist.

**Why:** The operational question is “what do we care about, and who should know when it appears?” rather than “what does each user individually subscribe to?”

**Status:** Accepted

---

## D003 — Standard Alert Schema
**Decision:** Every source should output the same normalized alert object before entering the central router.

**Why:** Source-specific parsing should remain in source workflows; routing logic should not understand every upstream API.

**Status:** Accepted

---

## D004 — Local GIS
**Decision:** Static/slow-changing GIS should be downloaded and stored locally in PostGIS instead of queried live during incidents.

**Why:** Faster local queries, fewer runtime dependencies, and resilience when upstream GIS services are unavailable.

**Status:** Accepted

---

## D005 — n8n is the automation engine, not the interface
**Decision:** Normal monitoring and data management should eventually happen through City Manager OS, not inside the n8n editor.

**Why:** n8n is excellent for automation but not for a live municipal operating picture.

**Status:** Accepted

---

## D006 — ntfy is the interruption layer
**Decision:** The dashboard shows the full picture; ntfy should interrupt only when watchlist/rules determine something deserves attention.

**Status:** Accepted

---

## D007 — Separate operational data from project documentation
**Decision:** GitHub contains architecture, sanitized templates, schemas, and workflow exports; confidential/live operational data remains on controlled infrastructure.

**Status:** Accepted

---

## D008 — Start with one end-to-end source
**Decision:** Prove the complete architecture with PSEG before migrating every source.

**Why:** A working end-to-end pattern is more valuable than partially converting many feeds at once.

**Status:** Accepted
