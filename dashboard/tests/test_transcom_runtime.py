from __future__ import annotations

import json
import time

import integration_runtime
import transcom_runtime
from integration_runtime import FetchResult


def result(payload):
    text = json.dumps(payload)

    return FetchResult(
        ok=True,
        status_code=200,
        final_url=(
            "https://de.infosensedigital.com"
            "/ISGDE/api/token/v4/get"
        ),
        elapsed_ms=5,
        headers={},
        body_text=text,
        body_bytes=len(text),
        truncated=False,
        content_type="application/json",
        error=None,
    )


def test_transcom_documented_response():
    expires = int(
        (time.time() + 900) * 1000
    )

    token, expires_at = (
        transcom_runtime._parse_token_response(
            result(
                {
                    "statusCode": 200,
                    "entity": {
                        "issueAt": int(
                            time.time() * 1000
                        ),
                        "expireAt": expires,
                        "token": "test-token",
                    },
                    "message": (
                        "Token generated successfully"
                    ),
                }
            )
        )
    )

    assert token == "test-token"
    assert expires_at > time.time()


def test_transcom_capitalized_schema():
    expires = int(
        (time.time() + 900) * 1000
    )

    token, _ = (
        transcom_runtime._parse_token_response(
            result(
                {
                    "Response": {
                        "StatusCode": 200,
                        "Entity": {
                            "IssueAt": int(
                                time.time() * 1000
                            ),
                            "ExpireAt": expires,
                            "Token": "schema-token",
                        },
                        "Message": (
                            "Token generated successfully"
                        ),
                    }
                }
            )
        )
    )

    assert token == "schema-token"


def test_transcom_cache(
    monkeypatch,
    tmp_path,
):
    calls = {"count": 0}

    monkeypatch.setenv(
        "TRANSCOM_TOKEN_CACHE_DIR",
        str(tmp_path),
    )

    def fake_request(**kwargs):
        calls["count"] += 1

        expires = int(
            (time.time() + 900) * 1000
        )

        return result(
            {
                "statusCode": 200,
                "entity": {
                    "issueAt": int(
                        time.time() * 1000
                    ),
                    "expireAt": expires,
                    "token": "cached-token",
                },
            }
        )

    monkeypatch.setattr(
        transcom_runtime,
        "perform_http_request",
        fake_request,
    )

    first = (
        transcom_runtime
        .get_cached_transcom_token(
            username="test-user",
            password="test-password",
        )
    )

    second = (
        transcom_runtime
        .get_cached_transcom_token(
            username="test-user",
            password="test-password",
        )
    )

    assert first == "cached-token"
    assert second == "cached-token"
    assert calls["count"] == 1


def test_integration_auth_query(
    monkeypatch,
):
    monkeypatch.setenv(
        "TEST_TRANSCOM_USERNAME",
        "test-user",
    )

    monkeypatch.setenv(
        "TEST_TRANSCOM_PASSWORD",
        "test-password",
    )

    monkeypatch.setenv(
        "TRANSCOM_BASE_URL",
        "https://de.infosensedigital.com",
    )

    monkeypatch.setattr(
        transcom_runtime,
        "get_cached_transcom_token",
        lambda **kwargs: "integration-token",
    )

    integration = {
        "auth_type": "TRANSCOM_TOKEN_ENV",
        "auth_config": {
            "username_env": (
                "TEST_TRANSCOM_USERNAME"
            ),
            "password_env": (
                "TEST_TRANSCOM_PASSWORD"
            ),
            "key_name": "token",
        },
    }

    headers = {}
    query = {}

    secrets = (
        integration_runtime
        .integration_auth_from_env(
            integration,
            headers,
            query,
        )
    )

    assert query["token"] == (
        "integration-token"
    )

    assert headers == {}

    assert "test-password" in secrets
    assert "integration-token" in secrets
