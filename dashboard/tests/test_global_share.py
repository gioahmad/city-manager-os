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


def test_global_share_uses_native_clients_and_smsgate_contract(monkeypatch, tmp_path):
    captured = {}

    def fake_request(**kwargs):
        captured.update(kwargs)
        return FetchResult(True, 202, kwargs["url"], 1, {}, "{}", 2, False, "application/json")

    monkeypatch.setenv("CMOS_SMSGATE_URL", "https://gateway.example/message")
    monkeypatch.setenv("CMOS_SMSGATE_USERNAME", "gateway-user")
    monkeypatch.setenv("CMOS_SMSGATE_PASSWORD", "gateway-password")
    monkeypatch.setenv("CMOS_SMSGATE_CONFIG_FILE", str(tmp_path / "smsgate.json"))
    monkeypatch.setattr(operations_app, "perform_http_request", fake_request)

    result = operations_app._send_smsgate("+1 (201) 555-1234", "Road closed")
    payload = json.loads(captured["body"])
    expected_auth = base64.b64encode(b"gateway-user:gateway-password").decode()

    assert result.status_code == 202
    assert payload == {"textMessage": {"text": "Road closed"}, "phoneNumbers": ["+12015551234"]}
    assert captured["headers"]["Authorization"] == f"Basic {expected_auth}"
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["User-Agent"] == "CityManagerOS/1.0"
    assert captured["allow_redirects"] is False
    assert captured["allow_private"] is True

    template = (DASHBOARD_ROOT / "templates" / "share.html").read_text()
    nav = (DASHBOARD_ROOT / "templates" / "nav.html").read_text()
    assert "mailto:" in template and "sms:" in template
    assert template.count('type="password"') == 1
    assert nav.count('href="/share"') == 2
    assert "CMOS_SMSGATE_USERNAME" in Path(operations_app.__file__).read_text()
    assert operations_app._normalize_smsgate_url(
        "https://api.sms-gate.app/3rdparty/v1/message"
    ) == "https://api.sms-gate.app/3rdparty/v1/messages"
    assert operations_app._normalize_smsgate_url(
        "http://192.168.1.20:8080/message"
    ) == "http://192.168.1.20:8080/message"


def test_smsgate_web_settings_are_private_and_alerts_are_shareable(monkeypatch, tmp_path):
    config_file = tmp_path / "smsgate.json"
    monkeypatch.setenv("CMOS_SMSGATE_CONFIG_FILE", str(config_file))
    operations_app._save_smsgate_settings(
        "https://gateway.example/message", "new-user", "new-secret"
    )

    assert operations_app._smsgate_settings() == {
        "url": "https://gateway.example/message",
        "username": "new-user",
        "password": "new-secret",
    }
    assert config_file.stat().st_mode & 0o777 == 0o600
    share_template = (DASHBOARD_ROOT / "templates" / "share.html").read_text()
    alerts_template = (DASHBOARD_ROOT / "templates" / "alerts.html").read_text()
    map_template = (DASHBOARD_ROOT / "templates" / "map.html").read_text()
    assert "new-secret" not in share_template
    assert "Share This Alert" in alerts_template
    assert "Share This Alert" in map_template

    monkeypatch.setattr(
        operations_app,
        "query_one",
        lambda *_args, **_kwargs: {
            "title": "Road closed",
            "message": "Use another route",
            "source": "BNN",
            "category": "TRAFFIC",
            "alert_id": "BNN:test",
            "received_at": None,
            "click_url": "https://example.test/alert",
            "location_label": "150 Park Street",
        },
    )
    subject, message = operations_app._alert_share_content("BNN:test")
    assert subject == "Alert: Road closed"
    assert "Location: 150 Park Street" in message
    assert "Reference: BNN:test" in message
