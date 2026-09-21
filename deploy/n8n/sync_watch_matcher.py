#!/usr/bin/env python3
"""Embed the shared Watch evaluator into both tracked central matcher definitions."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MATCHER_SOURCE = ROOT / "dashboard/static/watch_matcher.js"
ADAPTER_SOURCE = ROOT / "deploy/n8n/watch_matcher_adapter.js"
WORKFLOWS = (
    ROOT / "workflows/core/CORE_Watchlist_Matcher_v1.json",
    ROOT / "workflows/live/CORE_Watchlist_Matcher_live.json",
)


def workflow(payload):
    return payload[0] if isinstance(payload, list) else payload


def main() -> None:
    code = MATCHER_SOURCE.read_text().rstrip() + "\n\n" + ADAPTER_SOURCE.read_text().lstrip()
    for path in WORKFLOWS:
        payload = json.loads(path.read_text())
        node = next(
            node
            for node in workflow(payload)["nodes"]
            if node.get("name") == "Match + Resolve Recipients"
        )
        node["parameters"]["jsCode"] = code
        path.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
        print(f"SYNCED {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
