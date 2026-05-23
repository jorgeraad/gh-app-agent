#!/usr/bin/env python3
"""
register-app.py — Drive GitHub's App Manifest flow end-to-end.

Registers a new GitHub App against your account, downloads its private key,
installs it on repos you select, and captures the installation ID.
The browser opens twice (Create + Install). Everything else is automatic.

Usage:
  bin/register-app.py --name <app-name> [--port 8765]

Outputs:
  apps/<app-name>/private-key.pem  (chmod 600)
  apps/<app-name>/config.env       (APP_ID, APP_SLUG, OWNER, INSTALLATION_ID)
  apps/default -> <app-name>       (symlink, only if no default exists)
"""
import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
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
    import requests
except ImportError:
    sys.exit(
        "ERROR: 'requests' is not installed.\n"
        f"Create the venv first:\n"
        f"  python3 -m venv {REPO_ROOT}/venv\n"
        f"  {REPO_ROOT}/venv/bin/pip install --upgrade pip pyjwt cryptography requests"
    )

APPS_DIR = REPO_ROOT / "apps"
MANIFEST_TEMPLATE = REPO_ROOT / "manifest-template.json"
SHUTDOWN_TIMEOUT_SECONDS = 600


def render_manifest(name: str, port: int) -> dict:
    raw = MANIFEST_TEMPLATE.read_text()
    rendered = raw.replace("{{NAME}}", name).replace("{{PORT}}", str(port))
    return json.loads(rendered)


class FlowState:
    def __init__(self, name: str, port: int, manifest: dict):
        self.name = name
        self.port = port
        self.manifest = manifest
        self.app_dir = APPS_DIR / name
        self.app_slug: str | None = None
        self.app_id: int | None = None
        self.owner: str | None = None
        self.install_url: str | None = None
        self.installation_id: str | None = None
        self.shutdown_event = threading.Event()
        self.error: str | None = None


def make_handler(state: FlowState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code: int, body: str, content_type: str = "text/html; charset=utf-8"):
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)

            if parsed.path == "/start":
                self._render_start()
            elif parsed.path == "/callback":
                self._handle_callback(params)
            elif parsed.path == "/installed":
                self._handle_installed(params)
            else:
                self._send(404, "<p>Not found.</p>")

        def _render_start(self):
            manifest_json = json.dumps(state.manifest)
            escaped = html.escape(manifest_json, quote=True)
            body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Registering GitHub App…</title></head>
<body style="font-family: -apple-system, sans-serif; max-width: 36rem; margin: 4rem auto; line-height: 1.5;">
<h2>Registering GitHub App…</h2>
<p>You will be taken to GitHub to confirm the App's permissions and name. After you click <b>"Create GitHub App"</b>, you'll be brought back here briefly and then sent to the install page.</p>
<form id="f" method="post" action="https://github.com/settings/apps/new">
  <input type="hidden" name="manifest" value="{escaped}">
  <button type="submit">Continue to GitHub</button>
</form>
<script>document.getElementById('f').submit();</script>
</body></html>"""
            self._send(200, body)

        def _handle_callback(self, params):
            code = (params.get("code") or [None])[0]
            if not code:
                state.error = "callback missing ?code"
                self._send(400, "<p>Missing ?code parameter.</p>")
                state.shutdown_event.set()
                return
            try:
                r = requests.post(
                    f"https://api.github.com/app-manifests/{code}/conversions",
                    headers={"Accept": "application/vnd.github+json"},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                state.error = f"manifest exchange failed: {e}"
                self._send(500, f"<p>Manifest exchange failed: {html.escape(str(e))}</p>")
                state.shutdown_event.set()
                return

            try:
                state.app_dir.mkdir(parents=True, exist_ok=False)
                key_path = state.app_dir / "private-key.pem"
                key_path.write_text(data["pem"])
                os.chmod(key_path, 0o600)

                state.app_id = data["id"]
                state.app_slug = data["slug"]
                state.owner = data["owner"]["login"]

                config_path = state.app_dir / "config.env"
                config_path.write_text(
                    f"GITHUB_APP_ID={data['id']}\n"
                    f"GITHUB_APP_SLUG={data['slug']}\n"
                    f"GITHUB_APP_NAME={data['name']}\n"
                    f"GITHUB_OWNER={data['owner']['login']}\n"
                )
                os.chmod(config_path, 0o600)
            except Exception as e:
                state.error = f"failed writing app files: {e}"
                self._send(500, f"<p>Failed to save credentials: {html.escape(str(e))}</p>")
                state.shutdown_event.set()
                return

            state.install_url = f"https://github.com/apps/{data['slug']}/installations/new"
            body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>App created</title></head>
<body style="font-family: -apple-system, sans-serif; max-width: 36rem; margin: 4rem auto; line-height: 1.5;">
<h2>App created.</h2>
<p>Redirecting to the install page where you'll pick which repositories the App can access…</p>
<script>window.location='{html.escape(state.install_url, quote=True)}';</script>
<p>If your browser does not redirect automatically, <a href="{html.escape(state.install_url, quote=True)}">click here</a>.</p>
</body></html>"""
            self._send(200, body)

        def _handle_installed(self, params):
            installation_id = (params.get("installation_id") or [None])[0]
            if not installation_id:
                state.error = "install callback missing installation_id"
                self._send(400, "<p>Missing installation_id.</p>")
                state.shutdown_event.set()
                return

            try:
                with (state.app_dir / "config.env").open("a") as f:
                    f.write(f"GITHUB_INSTALLATION_ID={installation_id}\n")
                state.installation_id = installation_id

                default_link = APPS_DIR / "default"
                if not default_link.is_symlink() and not default_link.exists():
                    default_link.symlink_to(state.name)
            except Exception as e:
                state.error = f"failed finalizing install: {e}"
                self._send(500, f"<p>Failed: {html.escape(str(e))}</p>")
                state.shutdown_event.set()
                return

            body = """<!doctype html>
<html><head><meta charset="utf-8"><title>Installed</title></head>
<body style="font-family: -apple-system, sans-serif; max-width: 36rem; margin: 4rem auto; line-height: 1.5;">
<h2>✓ Installed</h2>
<p>Setup complete. You can close this tab and return to your terminal.</p>
</body></html>"""
            self._send(200, body)
            state.shutdown_event.set()

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True, help="App name (must be globally unique on GitHub)")
    parser.add_argument("--port", type=int, default=8765, help="Local callback port (default: 8765)")
    args = parser.parse_args()

    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,33}$", args.name):
        sys.exit(f"ERROR: invalid app name '{args.name}'. Use 1-34 chars: letters, digits, dashes, underscores; must start with a letter or digit.")

    APPS_DIR.mkdir(exist_ok=True)
    if (APPS_DIR / args.name).exists():
        sys.exit(f"ERROR: {APPS_DIR / args.name} already exists. Use a different --name, or remove the directory if you want to re-register.")

    manifest = render_manifest(args.name, args.port)
    state = FlowState(args.name, args.port, manifest)

    try:
        httpd = HTTPServer(("localhost", args.port), make_handler(state))
    except OSError as e:
        sys.exit(f"ERROR: could not bind localhost:{args.port} ({e}). Try a different --port.")

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    start_url = f"http://localhost:{args.port}/start"
    print(f"Opening {start_url} in your default browser.")
    print()
    print("⚠  The App will be registered against whichever GitHub account is logged in")
    print("   in your default browser. If you have multiple accounts, verify the right")
    print("   one is active BEFORE clicking 'Create GitHub App'.")
    print()
    print("You'll be asked to confirm twice:")
    print("  1. 'Create GitHub App from manifest' — registers the app on your account")
    print("  2. 'Install <app-name>' — picks which repos the app can access")
    print()
    print("Waiting for browser flow to complete…")

    webbrowser.open(start_url)

    completed = state.shutdown_event.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    if not completed:
        httpd.shutdown()
        sys.exit(f"\nTimed out after {SHUTDOWN_TIMEOUT_SECONDS}s without completing the install. "
                 "If you partially completed it, remove apps/<name>/ and re-run.")

    httpd.shutdown()
    server_thread.join(timeout=5)

    if state.error:
        sys.exit(f"\nSetup failed: {state.error}\n"
                 f"Inspect apps/{args.name}/ (if it exists) and remove it before retrying.")

    print()
    print(f"✓ App registered and installed.")
    print(f"  Name:            {args.name}")
    print(f"  Slug:            {state.app_slug}")
    print(f"  App ID:          {state.app_id}")
    print(f"  Installation ID: {state.installation_id}")
    print(f"  Owner:           {state.owner}")
    print(f"  App dir:         {state.app_dir}")
    default_link = APPS_DIR / "default"
    if default_link.is_symlink():
        print(f"  Default app:     {os.readlink(default_link)}")
    print()
    print("Next steps:")
    print("  1. Run bin/mint-token.py to verify token minting works.")
    print("  2. Configure git's credential helper (see README).")
    print("  3. (Recommended) Set branch protection on main of each repo in the GitHub UI.")


if __name__ == "__main__":
    main()
