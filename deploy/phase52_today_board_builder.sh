#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="0e0bb3147f196f9c440017eae635a0bda988877b"
BRANCH="feature/daily-constants-today-board"
SELF="deploy/phase52_today_board_builder.sh"
TARGET="deploy/phase52_today_board.sh"

cd "$REPO"

echo "============================================================"
echo "#52 TODAY BOARD WIRING BUILDER"
echo "============================================================"

git fetch origin main "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"

[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]
[ -z "$(git status --porcelain)" ]

python3 - <<'PY'
from pathlib import Path

# Docker image includes new modules.
p = Path("dashboard/Dockerfile")
s = p.read_text()
old = "COPY operations_routines_app.py .\nCOPY operations_occurrence_controls.py .\n"
new = "COPY operations_routines_app.py .\nCOPY today_board_engine.py .\nCOPY today_board_app.py .\nCOPY operations_occurrence_controls.py .\n"
if old not in s:
    raise SystemExit("Dockerfile anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)

# Import Today Board after all route modules so its My Day / Overview wrappers win.
p = Path("dashboard/phase3_app.py")
s = p.read_text()
old = "# Upgrades Alert Admin in place while preserving the existing matcher and routing tables.\nimport alert_admin_v2  # noqa: F401,E402\n\nfrom private_auth import configure_private_auth\n"
new = "# Upgrades Alert Admin in place while preserving the existing matcher and routing tables.\nimport alert_admin_v2  # noqa: F401,E402\n# Adds #52 Daily Constants / Today Board after other route modules are registered.\nimport today_board_app  # noqa: F401,E402\n\nfrom private_auth import configure_private_auth\n"
if old not in s:
    raise SystemExit("phase3_app anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)

# Executive-only writes for Today Board configuration.
p = Path("dashboard/private_auth.py")
s = p.read_text()
old = '    "/rules",\n    "/alert-admin",\n'
new = '    "/rules",\n    "/today-board",\n    "/alert-admin",\n'
if old not in s:
    raise SystemExit("private_auth admin prefix anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)

# Navigation entry in desktop and mobile configuration menus.
p = Path("dashboard/templates/nav.html")
s = p.read_text()
old = "request.url.path.startswith('/rules') or request.url.path.startswith('/watchlist')"
new = "request.url.path.startswith('/rules') or request.url.path.startswith('/today-board') or request.url.path.startswith('/watchlist')"
if old not in s:
    raise SystemExit("nav active-state anchor missing")
s = s.replace(old, new, 1)
old = '        <a href="/rules">Rules Center</a>\n        <a href="/watchlist">Watchlists</a>\n'
new = '        <a href="/rules">Rules Center</a>\n        <a href="/today-board">Today Board</a>\n        <a href="/watchlist">Watchlists</a>\n'
if old not in s:
    raise SystemExit("desktop nav anchor missing")
s = s.replace(old, new, 1)
old = '      <a href="/rules">Rules Center</a>\n      <a href="/watchlist">Watchlists</a>\n'
new = '      <a href="/rules">Rules Center</a>\n      <a href="/today-board">Today Board</a>\n      <a href="/watchlist">Watchlists</a>\n'
if old not in s:
    raise SystemExit("mobile nav anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)

# Put the compact overview board inside <main>, immediately before metrics.
p = Path("dashboard/today_board_app.py")
s = p.read_text()
old = '    return _inject(response, "<main>", context)\n'
new = '    return _inject(response, \'<section class="metrics metrics-six"\', context)\n'
if old not in s:
    raise SystemExit("overview injection anchor missing")
s = s.replace(old, new, 1)
p.write_text(s)
PY

python3 -m py_compile dashboard/today_board_engine.py dashboard/today_board_app.py dashboard/phase3_app.py
bash -n "$TARGET"
grep -Fq 'import today_board_app' dashboard/phase3_app.py
grep -Fq 'COPY today_board_app.py' dashboard/Dockerfile
grep -Fq '"/today-board"' dashboard/private_auth.py
grep -Fq 'href="/today-board"' dashboard/templates/nav.html
grep -Fq 'metrics metrics-six' dashboard/today_board_app.py

chmod +x "$TARGET"
git add \
  dashboard/Dockerfile \
  dashboard/phase3_app.py \
  dashboard/private_auth.py \
  dashboard/templates/nav.html \
  dashboard/today_board_app.py \
  "$TARGET"
git rm -q "$SELF"
git diff --cached --check
git commit -m "Wire Daily Constants Today Board"
git push origin "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

echo "Prepared feature HEAD: $(git rev-parse HEAD)"
echo "Today Board wiring: PASS"
echo
exec bash "$TARGET"
