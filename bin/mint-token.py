#!/usr/bin/env python3
"""
mint-token.py — Mint (and cache) a GitHub App installation token.

Resolves which app to use from $GH_AGENT_APP (default: apps/default symlink).
Reads APP_ID, INSTALLATION_ID, and the private key. Uses cached token from
.token-cache.json if it still has >60s of life; otherwise mints a fresh one
via the GitHub API and writes a new cache.

Prints the token (only) to stdout. Errors go to stderr with non-zero exit.

Usage:
  bin/mint-token.py                    # uses apps/default
  GH_AGENT_APP=other bin/mint-token.py # uses apps/other
"""
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python3"

if VENV_PYTHON.exists():
    try:
        same = Path(sys.executable).resolve() == VENV_PYTHON.resolve()
    except OSError:
        same = False
    if not same:
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON)] + sys.argv)

try:
    import jwt
    import requests
except ImportError:
    sys.exit(
        "ERROR: 'pyjwt' or 'requests' not installed.\n"
        f"  python3 -m venv {REPO_ROOT}/venv\n"
        f"  {REPO_ROOT}/venv/bin/pip install --upgrade pip pyjwt cryptography requests"
    )

APPS_DIR = REPO_ROOT / "apps"
CACHE_REFRESH_THRESHOLD_SECONDS = 60
JWT_EXPIRY_SECONDS = 540  # 9 minutes; GitHub allows up to 10
INSTALLATION_TOKEN_URL = "https://api.github.com/app/installations/{installation_id}/access_tokens"


def resolve_app_dir() -> Path:
    name = os.environ.get("GH_AGENT_APP", "default")
    candidate = APPS_DIR / name
    if not candidate.exists():
        sys.exit(
            f"ERROR: app dir not found: {candidate}\n"
            "  - Set $GH_AGENT_APP to the name of a registered app under apps/, or\n"
            "  - Create apps/default symlink, or\n"
            "  - Run bin/register-app.py --name <new-app>"
        )
    return candidate.resolve()


def read_config(app_dir: Path) -> dict:
    config_path = app_dir / "config.env"
    if not config_path.exists():
        sys.exit(f"ERROR: missing {config_path}. Re-run bin/register-app.py.")
    config = {}
    for line in config_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        config[k.strip()] = v.strip()
    for required in ("GITHUB_APP_ID", "GITHUB_INSTALLATION_ID"):
        if required not in config:
            sys.exit(f"ERROR: {required} missing from {config_path}.")
    return config


def cached_token_if_valid(cache_path: Path) -> str | None:
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    token = data.get("token")
    expires_at = data.get("expires_at_epoch")
    if not token or not expires_at:
        return None
    if expires_at - time.time() <= CACHE_REFRESH_THRESHOLD_SECONDS:
        return None
    return token


def parse_iso8601(s: str) -> float:
    from datetime import datetime
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def mint_fresh_token(app_dir: Path, config: dict) -> tuple[str, float]:
    key_path = app_dir / "private-key.pem"
    if not key_path.exists():
        sys.exit(f"ERROR: missing private key at {key_path}. Re-run bin/register-app.py.")
    private_key = key_path.read_text()

    now = int(time.time())
    payload = {
        "iat": now - 60,  # 60s clock-skew tolerance
        "exp": now + JWT_EXPIRY_SECONDS,
        "iss": int(config["GITHUB_APP_ID"]),
    }
    app_jwt = jwt.encode(payload, private_key, algorithm="RS256")

    url = INSTALLATION_TOKEN_URL.format(installation_id=config["GITHUB_INSTALLATION_ID"])
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    if resp.status_code != 201:
        sys.exit(f"ERROR: GitHub returned {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    token = data["token"]
    expires_at_epoch = parse_iso8601(data["expires_at"])
    return token, expires_at_epoch


def write_cache(cache_path: Path, token: str, expires_at_epoch: float) -> None:
    payload = json.dumps({"token": token, "expires_at_epoch": expires_at_epoch})
    # Write+chmod atomically: write to temp, chmod, rename
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(payload)
    os.chmod(tmp, 0o600)
    tmp.replace(cache_path)


def main():
    app_dir = resolve_app_dir()
    config = read_config(app_dir)
    cache_path = app_dir / ".token-cache.json"

    cached = cached_token_if_valid(cache_path)
    if cached:
        print(cached)
        return

    token, expires_at_epoch = mint_fresh_token(app_dir, config)
    write_cache(cache_path, token, expires_at_epoch)
    print(token)


if __name__ == "__main__":
    main()
