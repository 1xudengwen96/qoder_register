"""Check a local BitBrowser API and CDP connection without running site workflows.

Usage: python tools/check_browser_connection.py --env-id EXISTING_ENV_ID
The selected environment stays open. No profile settings are changed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from urllib.parse import urlsplit


class ConnectionCheckError(RuntimeError):
    """A diagnostic message that contains no raw API data."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:54345")
    parser.add_argument("--env-id", help="Existing environment to open with a blank page")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("--base must be a local HTTP address")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path: str, body: dict, timeout: int = 15) -> dict:
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("BITBROWSER_TOKEN")
        if token:
            headers["Authorization"] = token
        request = urllib.request.Request(
            base + path, data=json.dumps(body).encode("utf-8"), headers=headers,
        )
        with opener.open(request, timeout=timeout) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get("success") is not True:
            message = str(result.get("msg", "")) if isinstance(result, dict) else ""
            if "dynamicIpUrl" in message:
                raise ConnectionCheckError("Environment cannot start: its current proxy settings require dynamicIpUrl")
            if isinstance(result, dict) and result.get("code") == 11018:
                raise ConnectionCheckError("Environment cannot start: API/RPA launch permission is restricted (11018)")
            raise ConnectionCheckError(f"Local API rejected {path}; check the client API authentication and permissions")
        return result

    try:
        call("/health", {})
        print("PASS local API health")
        if not args.env_id:
            print("Provide --env-id to check an existing environment's CDP connection")
            return 2

        # Import before opening the browser so missing dependencies have no side effects.
        from playwright.sync_api import sync_playwright

        data = call("/browser/open", {
            "id": args.env_id, "queue": False, "ignoreDefaultUrls": True,
            "args": ["about:blank"],
        }, timeout=120).get("data") or {}
        endpoint = data.get("http") or data.get("ws")
        if not isinstance(endpoint, str) or not endpoint:
            raise ConnectionCheckError("Local API returned no CDP endpoint")
        if "://" not in endpoint:
            endpoint = "http://" + endpoint
        address = urlsplit(endpoint)
        if address.scheme not in ("http", "ws") or address.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ConnectionCheckError("Local API returned a non-local CDP endpoint")
        print("PASS browser opened; local CDP endpoint received")
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(endpoint, timeout=30000)
            session = browser.new_browser_cdp_session()
            try:
                version = session.send("Browser.getVersion")
                print("PASS Playwright CDP connection")
                print(json.dumps({k: version.get(k) for k in (
                    "product", "protocolVersion", "jsVersion",
                )}, indent=2))
            finally:
                session.detach()
            # Stopping Playwright disconnects this client and leaves the browser open.
        print("PASS disconnected; selected browser remains open")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Avoid dumping remote responses or credentials into terminal output.
        print(f"FAIL connection check ({type(exc).__name__})", file=sys.stderr)
        if isinstance(exc, ConnectionCheckError):
            print(str(exc), file=sys.stderr)
        print("Check that BitBrowser is running, the environment ID is valid, and API/RPA access is enabled.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
