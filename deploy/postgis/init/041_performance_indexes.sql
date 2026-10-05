BEGIN;

-- Activity chronology is now the operational sort/window contract throughout
-- Alerts, Workspace, What Changed and Mapping Center.
CREATE INDEX IF NOT EXISTS idx_alerts_activity_time
  ON alerts ((coalesce(observed_at,received_at)) DESC, received_at DESC);

-- Context and Watch inspectors repeatedly read recent matches from either side.
CREATE INDEX IF NOT EXISTS idx_alert_watch_matches_alert_recent
  ON alert_watch_matches(alert_id, matched_at DESC);

CREATE INDEX IF NOT EXISTS idx_alert_watch_matches_watch_recent
  ON alert_watch_matches(watch_item_id, matched_at DESC);

-- Notification history / exception brief use recent delivery state.
CREATE INDEX IF NOT EXISTS idx_deliveries_created_recent
  ON deliveries(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_deliveries_status_attempted_recent
  ON deliveries(status, attempted_at DESC);

-- Universal Context traverses this existing graph in both directions.
CREATE INDEX IF NOT EXISTS idx_workspace_context_links_source_recent
  ON workspace_context_links(owner_username,source_kind,source_id,created_at DESC);

CREATE INDEX IF NOT EXISTS idx_workspace_context_links_target_recent
  ON workspace_context_links(owner_username,target_kind,target_id,created_at DESC);

-- Executive exception / Waiting On views independently filter these dates.
CREATE INDEX IF NOT EXISTS idx_issues_open_due
  ON issues(due_at)
  WHERE status NOT IN ('RESOLVED','CLOSED') AND due_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_issues_open_follow_up
  ON issues(follow_up_at)
  WHERE status NOT IN ('RESOLVED','CLOSED') AND follow_up_at IS NOT NULL;

COMMIT;
