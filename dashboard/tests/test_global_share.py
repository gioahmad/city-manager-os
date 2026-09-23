import base64
import json
import os
import sys
from pathlib import Path


DASHBOARD_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DASHBOARD_ROOT))
os.environ.setdefault("DB_PASSWORD", "test")

import operations_app
from integration_runtime import FetchResult


def test_global_share_uses_native_clients_and_smsgate_contract(monkeypatch):
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return FetchResult(True, 202, kwargs["url"], 1, {}, "{}", 2, False, "application/json")

    monkeypatch.setenv("CMOS_SMSGATE_URL", "https://gateway.example/message")
    monkeypatch.setenv("CMOS_SMSGATE_USERNAME", "gateway-user")
    monkeypatch.setenv("CMOS_SMSGATE_PASSWORD", "gateway-password")
    monkeypatch.setattr(operations_app, "perform_http_request", fake_request)

    result = operations_app._send_smsgate("+1 (201) 555-1234", "Road closed")
    payload = json.loads(captured["body"])
    expected_auth = base64.b64encode(b"gateway-user:gateway-password").decode()

    assert result.status_code == 202
    assert payload == {"textMessage": {"text": "Road closed"}, "phoneNumbers": ["+12015551234"]}
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["allow_redirects"] is False
    assert captured["allow_private"] is True

    template = (DASHBOARD_ROOT / "templates" / "share.html").read_text()
    nav = (DASHBOARD_ROOT / "templates" / "nav.html").read_text()
    assert "mailto:" in template and "sms:" in template
    assert template.count("CMOS_SMSGATE_PASSWORD=") == 1
    assert nav.count('href="/share"') == 2
    assert "CMOS_SMSGATE_USERNAME" in Path(operations_app.__file__).read_text()
