import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DB_PASSWORD", "test")
import private_auth as auth


def test_private_form_policy_preserves_origin_without_accepting_null(monkeypatch):
    origin = "https://dashboard.example"
    monkeypatch.setenv("CMOS_AUTH_ENABLED", "true")
    monkeypatch.setenv("CMOS_PUBLIC_ORIGIN", origin)
    monkeypatch.setenv("CMOS_SESSION_SECRET", "origin-regression-secret-at-least-32-characters")
    monkeypatch.setenv("CMOS_EXECUTIVE_USERNAME", "OriginTest")
    monkeypatch.setenv("CMOS_EXECUTIVE_PASSWORD_HASH", "unused-for-issued-session")
    monkeypatch.delenv("CMOS_AUTOMATION_TOKEN", raising=False)
    app = FastAPI()
    created = []

    @app.get("/watchlist", response_class=HTMLResponse)
    def form():
        return '<form method="post" action="/watchlist/create"><button>Create</button></form>'

    @app.post("/watchlist/create")
    def create():
        created.append(True)
        return {"created": True}

    auth.configure_private_auth(app)
    with TestClient(app, base_url=origin) as client:
        account = auth.Account("OriginTest", "EXECUTIVE", "")
        client.cookies.set(auth.COOKIE_NAME, auth._issue_session(account))
        response = client.get("/watchlist")
        assert response.status_code == 200
        assert response.headers["Referrer-Policy"] == "same-origin"
        assert client.post("/watchlist/create", headers={"Origin": origin}).status_code == 200
        for rejected in ("null", "https://attacker.example"):
            response = client.post("/watchlist/create", headers={"Origin": rejected})
            assert response.status_code == 403
            assert response.text == "Invalid request origin."
        assert created == [True]
