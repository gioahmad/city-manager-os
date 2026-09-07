#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="/opt/city-manager-os"
ENV_FILE="$ROOT/dashboard/.env"
CREDENTIAL_FILE="/root/.cmos-executive-first-login"
ROTATE_PASSWORD=0

if [[ "${1:-}" == "--rotate-password" ]]; then
  ROTATE_PASSWORD=1
elif [[ -n "${1:-}" ]]; then
  echo "Usage: $0 [--rotate-password]"
  exit 2
fi

[[ -f "$ENV_FILE" ]] || {
  echo "ERROR: $ENV_FILE does not exist"
  exit 1
}

python3 - "$ENV_FILE" "$CREDENTIAL_FILE" "$ROTATE_PASSWORD" <<'PY'
from __future__ import annotations

import hashlib
import os
import secrets
import sys
from pathlib import Path

ENV = Path(sys.argv[1])
CREDENTIAL = Path(sys.argv[2])
ROTATE = sys.argv[3] == "1"
ITERATIONS = 310_000


def read_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text().splitlines()
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt,
        ITERATIONS,
    )
    return f"pbkdf2_sha256:{ITERATIONS}:{salt.hex()}:{digest.hex()}"


def update_lines(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    if remaining:
        output.append("")
        output.append("# City Manager OS private dashboard authentication")
        for key, value in remaining.items():
            output.append(f"{key}={value}")
    return output


lines, values = read_env(ENV)
username = values.get("CMOS_EXECUTIVE_USERNAME", "").strip() or "gio"
existing_hash = values.get("CMOS_EXECUTIVE_PASSWORD_HASH", "").strip()

session_secret = values.get("CMOS_SESSION_SECRET", "").strip()
if len(session_secret) < 32:
    session_secret = secrets.token_hex(32)

automation_token = values.get("CMOS_AUTOMATION_TOKEN", "").strip()
if len(automation_token) < 32:
    automation_token = secrets.token_urlsafe(32)

new_password = None
if ROTATE or not existing_hash:
    new_password = secrets.token_urlsafe(18)
    existing_hash = password_hash(new_password)

updates = {
    "CMOS_AUTH_ENABLED": "true",
    "CMOS_AUTH_SECURE_COOKIES": values.get("CMOS_AUTH_SECURE_COOKIES", "false") or "false",
    "CMOS_AUTH_TRUST_PROXY": values.get("CMOS_AUTH_TRUST_PROXY", "false") or "false",
    "CMOS_PUBLIC_ORIGIN": values.get("CMOS_PUBLIC_ORIGIN", ""),
    "CMOS_SESSION_SECRET": session_secret,
    "CMOS_AUTOMATION_TOKEN": automation_token,
    "CMOS_EXECUTIVE_USERNAME": username,
    "CMOS_EXECUTIVE_PASSWORD_HASH": existing_hash,
    "CMOS_SUPERVISOR_USERNAME": values.get("CMOS_SUPERVISOR_USERNAME", ""),
    "CMOS_SUPERVISOR_PASSWORD_HASH": values.get("CMOS_SUPERVISOR_PASSWORD_HASH", ""),
    "CMOS_READONLY_USERNAME": values.get("CMOS_READONLY_USERNAME", ""),
    "CMOS_READONLY_PASSWORD_HASH": values.get("CMOS_READONLY_PASSWORD_HASH", ""),
    "CMOS_LOGIN_MAX_FAILURES": values.get("CMOS_LOGIN_MAX_FAILURES", "8") or "8",
    "CMOS_LOGIN_WINDOW_SECONDS": values.get("CMOS_LOGIN_WINDOW_SECONDS", "900") or "900",
}

ENV.write_text("\n".join(update_lines(lines, updates)).rstrip() + "\n")
os.chmod(ENV, 0o600)

if new_password is not None:
    CREDENTIAL.write_text(
        "City Manager OS private dashboard first-login credential\n"
        f"Username: {username}\n"
        f"Password: {new_password}\n"
        "\nDelete this file after the credential is stored securely.\n"
    )
    os.chmod(CREDENTIAL, 0o600)

print("PRIVATE AUTH BOOTSTRAP: PASS")
print(f"executive_username={username}")
print(f"credential_file={CREDENTIAL if new_password is not None else 'UNCHANGED'}")
print("supervisor_account=NOT_CONFIGURED")
print("readonly_account=NOT_CONFIGURED")
PY
