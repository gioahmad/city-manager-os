#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="ae07c387d7029c6b3bb833d56e7c4a3107a43ebc"
BRANCH="feature/executive-workflow-pack"
SELF="deploy/phase55_executive_workflow_builder.sh"
TARGET="deploy/phase55_executive_workflow_pack.sh"

cd "$REPO"

echo "============================================================"
echo "#55 EXECUTIVE WORKFLOW WIRING BUILDER"
echo "============================================================"

git fetch origin main "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"

[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ -z "$(git status --porcelain)" ]

python3 - <<'PY'
from pathlib import Path

# Docker image includes the executive workflow route module.
p = Path("dashboard/Dockerfile")
s = p.read_text()
old = "COPY today_board_engine.py .\nCOPY today_board_app.py .\nCOPY operations_occurrence_controls.py .\n"
new = "COPY today_board_engine.py .\nCOPY today_board_app.py .\nCOPY executive_workflow_app.py .\nCOPY operations_occurrence_controls.py .\n"
if old not in s:
    raise SystemExit("Dockerfile anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)

# Import #55 after #52 so the My Day wrapper composes on top of Today Board.
p = Path("dashboard/phase3_app.py")
s = p.read_text()
old = "# Adds #52 Daily Constants / Today Board after other route modules are registered.\nimport today_board_app  # noqa: F401,E402\n\nfrom private_auth import configure_private_auth\n"
new = "# Adds #52 Daily Constants / Today Board after other route modules are registered.\nimport today_board_app  # noqa: F401,E402\n# Adds #55 executive capture, inbox, What Changed, wizard and Waiting On tools.\nimport executive_workflow_app  # noqa: F401,E402\n\nfrom private_auth import configure_private_auth\n"
if old not in s:
    raise SystemExit("phase3_app anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)

# Navigation: desktop Inbox, executive links, setup wizard, mobile links, global capture control.
p = Path("dashboard/templates/nav.html")
s = p.read_text()
old = '    <a href="/issues" class="{% if request.url.path.startswith(\'/issues\') %}active{% endif %}">Command</a>\n'
new = old + '    <a href="/inbox" class="{% if request.url.path.startswith(\'/inbox\') %}active{% endif %}">Inbox</a>\n'
if old not in s:
    raise SystemExit("desktop Command anchor missing")
s = s.replace(old, new, 1)

old = '        <a href="/">Overview</a>\n        <a href="/modules">Modules</a>\n'
new = '        <a href="/">Overview</a>\n        <a href="/what-changed">What Changed</a>\n        <a href="/modules">Modules</a>\n'
if old not in s:
    raise SystemExit("desktop Overview anchor missing")
s = s.replace(old, new, 1)

old = '        <a href="/today-board">Today Board</a>\n        <a href="/watchlist">Watchlists</a>\n'
new = '        <a href="/today-board">Today Board</a>\n        <a href="/today-board/setup">Today Board Setup Wizard</a>\n        <a href="/watchlist">Watchlists</a>\n'
if old not in s:
    raise SystemExit("desktop Today Board anchor missing")
s = s.replace(old, new, 1)

old = '      <a href="/alerts">Alerts</a>\n      <a href="/">Overview</a>\n'
new = '      <a href="/alerts">Alerts</a>\n      <a href="/inbox">Inbox</a>\n      <a href="/what-changed">What Changed</a>\n      <a href="/">Overview</a>\n'
if old not in s:
    raise SystemExit("mobile Alerts anchor missing")
s = s.replace(old, new, 1)

old = '      <a href="/today-board">Today Board</a>\n      <a href="/watchlist">Watchlists</a>\n'
new = '      <a href="/today-board">Today Board</a>\n      <a href="/today-board/setup">Today Board Setup Wizard</a>\n      <a href="/watchlist">Watchlists</a>\n'
if old not in s:
    raise SystemExit("mobile Today Board anchor missing")
s = s.replace(old, new, 1)

if '{% include "quick_capture_global.html" %}' not in s:
    s = s.rstrip() + '\n\n{% include "quick_capture_global.html" %}\n'
p.write_text(s)

# Fast Waiting On entry point directly in each open Command Center card.
p = Path("dashboard/templates/issues.html")
s = p.read_text()
anchor = '            <form method="post" action="/issues/{{ i.id }}/update" class="watch-form compact">\n'
insert = '''            {% if not i.waiting_on and i.status not in ['RESOLVED','CLOSED'] %}
            <details class="optional-block" style="margin-bottom:12px">
              <summary>Quick Waiting On</summary>
              <form method="post" action="/issues/{{ i.id }}/quick-waiting" class="watch-form compact" style="margin-top:9px">
                <input type="hidden" name="return_to" value="/issues?state={{ state }}">
                <div class="form-grid two-col">
                  <label>Waiting On<input name="waiting_on" required placeholder="Person, agency, vendor..."></label>
                  <label>Chase / Follow Up<input type="datetime-local" name="follow_up_at"></label>
                </div>
                <button class="secondary-button" type="submit">Set Waiting On</button>
              </form>
            </details>
            {% endif %}
'''
if anchor not in s:
    raise SystemExit("issues quick-waiting anchor missing")
s = s.replace(anchor, insert + anchor, 1)
p.write_text(s)
PY

python3 -m py_compile dashboard/executive_workflow_app.py dashboard/phase3_app.py
bash -n "$TARGET"
grep -Fq 'import executive_workflow_app' dashboard/phase3_app.py
grep -Fq 'COPY executive_workflow_app.py' dashboard/Dockerfile
grep -Fq 'quick_capture_global.html' dashboard/templates/nav.html
grep -Fq 'Today Board Setup Wizard' dashboard/templates/nav.html
grep -Fq 'quick-waiting' dashboard/templates/issues.html

chmod +x "$TARGET"
git add \
  dashboard/Dockerfile \
  dashboard/phase3_app.py \
  dashboard/templates/nav.html \
  dashboard/templates/issues.html \
  "$TARGET"
git rm -q "$SELF"
git diff --cached --check
git commit -m "Wire executive workflow pack"
git push origin "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

echo "Prepared feature HEAD: $(git rev-parse HEAD)"
echo "Executive workflow wiring: PASS"
echo
exec bash "$TARGET"
