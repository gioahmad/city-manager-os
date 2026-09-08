#!/usr/bin/env bash
set -Eeuo pipefail

REPO="/opt/city-manager-os"
BASE="36833baf9e115d97a4a3fbab8eb08ff0eacaeddd"
BRANCH="feature/regional-event-source-pack-1"
EXPECTED="a964330b0b1dab7ca58da0d60d4ec2c73ddadc8d"
TARGET="deploy/phase35_source_pack_builder.sh"
SELF="deploy/phase35_builder_schema_hotfix.sh"

cd "$REPO"

echo "============================================================"
echo "#35 BUILDER JSON-LD SCHEMA HOTFIX"
echo "============================================================"

git fetch origin main "$BRANCH"
git switch -C "$BRANCH" "origin/$BRANCH"

[ "$(git rev-parse origin/main)" = "$BASE" ]
[ "$(git merge-base origin/main HEAD)" = "$BASE" ]
[ "$(git rev-parse HEAD)" = "$EXPECTED" ]
[ -z "$(git status --porcelain)" ]

python3 - <<'PY'
from pathlib import Path

p = Path("deploy/phase35_source_pack_builder.sh")
s = p.read_text()

old = '''def _jsonld_types(value: Any) -> set[str]:
    raw = value if isinstance(value, list) else [value]
    return {str(item).strip().lower() for item in raw if item is not None}


def _jsonld_walk(value: Any):
    if isinstance(value, dict):
        if "event" in _jsonld_types(value.get("@type")):
            yield value
        for child in value.values():
            yield from _jsonld_walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _jsonld_walk(child)
'''
new = '''def _jsonld_types(value: Any) -> set[str]:
    raw = value if isinstance(value, list) else [value]
    output: set[str] = set()
    for item in raw:
        if item is None:
            continue
        text = str(item).strip().lower()
        if not text:
            continue
        output.add(text)
        output.add(text.rsplit("/", 1)[-1].rsplit("#", 1)[-1])
    return output


def _jsonld_walk(value: Any):
    if isinstance(value, dict):
        types = _jsonld_types(value.get("@type"))
        schema_event = any(
            item == "event"
            or item.endswith("event")
            or item in {"festival", "hackathon"}
            for item in types
        )
        if schema_event:
            yield value
        for child in value.values():
            yield from _jsonld_walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _jsonld_walk(child)
'''
if old not in s:
    raise SystemExit("JSON-LD type anchor missing")
s = s.replace(old, new, 1)

old = '''                "venue": venue,
                "address": _jsonld_address_text(address_obj),
                "municipality": municipality,
                "state": state,
'''
new = '''                "venue": venue or defaults.get("venue"),
                "address": _jsonld_address_text(address_obj) or defaults.get("address"),
                "municipality": municipality or defaults.get("municipality"),
                "state": state or defaults.get("state"),
'''
if old not in s:
    raise SystemExit("JSON-LD defaults anchor missing")
s = s.replace(old, new, 1)

p.write_text(s)
PY

bash -n "$TARGET"
grep -Fq 'item.endswith("event")' "$TARGET"
grep -Fq 'venue or defaults.get("venue")' "$TARGET"

git add "$TARGET"
git rm -q "$SELF"
git diff --cached --check
git commit -m "Harden #35 JSON-LD event type handling"
git push origin "$BRANCH"

[ -z "$(git status --porcelain)" ]
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/$BRANCH)" ]

echo "Corrected staging HEAD: $(git rev-parse HEAD)"
echo "Schema hotfix: PASS"
echo
exec bash "$TARGET"
