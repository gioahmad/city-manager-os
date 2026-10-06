"""Verify the tracked BNN source classifier with variable pipe ordering using Node."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not shutil.which("node"):
    raise SystemExit("node is required for BNN classifier verification")

shell = (ROOT / "deploy/cmos-source").read_text()
workflow = json.loads((ROOT / "workflows/live/BNN_live.json").read_text())

def extract(after: str) -> str:
    start = shell.index(after) + len(after)
    end = shell.index('"""', start)
    return shell[start:end]

js = extract('HASH_JS = r"""') + extract('BNN_JS = HASH_JS + r"""')
tracked = next(node for node in workflow["nodes"] if node.get("name") == "Build Standard BNN Alert")["parameters"]["jsCode"]
assert tracked == js, "tracked BNN workflow drifted from deploy/cmos-source"

payloads = [
    {"body": {"formatted_message": "10/06/2026 1:00 PM | NJ | Hudson | Jersey City | Working Fire Alert | 123 Newark Ave | Second alarm. | nj101"}},
    {"body": {"formatted_message": "10/06/2026 1:05 PM | Hudson County | Working Fire Alert | NJ | Jersey City | 125 Newark Ave | Companies operating. | nj102"}},
    {"body": {"formatted_message": "10/06/2026 1:10 PM | Traffic Alert | Queens County | Astoria | NY | 31-00 47th Ave | MVA with injuries. | ny201"}},
    {"body": {"formatted_message": "10/06/2026 1:15 PM | NY | New York County | Manhattan | Police Alert | 350 5th Ave | Investigation. | ny202", "city": "Manhattan"}},
]
runner = f"""
const input = {json.dumps(payloads)};
global.$input = {{ all: () => input.map((json) => ({{json}})) }};
const result = (function() {{
{js}
}})();
process.stdout.write(JSON.stringify(result.map((item) => item.json)));
"""
result = subprocess.run(["node", "-e", runner], check=True, text=True, capture_output=True)
alerts = json.loads(result.stdout)

assert alerts[0]["location"]["state"] == "NJ"
assert alerts[0]["county"] == "Hudson"
assert "Hudson County" in alerts[0]["search_text"]
assert alerts[0]["municipality"] == "", "pipe position must not be trusted as municipality"

assert alerts[1]["location"]["state"] == "NJ"
assert alerts[1]["county"] == "Hudson"
assert alerts[1]["metadata"]["bnn_pipe_fields"]["order_trusted"] is False
assert alerts[1]["metadata"]["bnn_pipe_fields"]["segments"][1] == "Hudson County"

assert alerts[2]["location"]["state"] == "NY"
assert alerts[2]["county"] == "Queens"
assert "Queens County" in alerts[2]["search_text"]
assert alerts[2]["municipality"] == ""

assert alerts[3]["location"]["state"] == "NY"
assert alerts[3]["county"] == "New York"
assert alerts[3]["municipality"] == "Manhattan", "explicit JSON municipality must win"
assert alerts[3]["location"]["address"] == "350 5th Ave"

print("BNN PIPE CLASSIFIER: PASS — NJ/NY state and county recognized independent of pipe order")
print("BNN PIPE CLASSIFIER: PASS — municipality is never inferred by position; explicit source field wins")
print("BNN PIPE CLASSIFIER: PASS — tracked live workflow exactly matches deploy source")
