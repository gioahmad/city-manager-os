#!/usr/bin/env python3
"""Embed the dashboard's shared content formatter into the central ntfy sender."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FORMATTER = ROOT / "dashboard/static/alert_content.js"
ADAPTER = ROOT / "deploy/n8n/ntfy_content_adapter.js"
WORKFLOW = ROOT / "workflows/core/CORE_ntfy_Sender_v1.json"


def main():
    workflow = json.loads(WORKFLOW.read_text())
    node = next(node for node in workflow["nodes"] if node["name"] == "Prepare ntfy Requests")
    node["parameters"]["jsCode"] = FORMATTER.read_text().rstrip() + "\n\n" + ADAPTER.read_text().lstrip()
    WORKFLOW.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")
    print("SYNCED", WORKFLOW.relative_to(ROOT))


if __name__ == "__main__":
    main()
