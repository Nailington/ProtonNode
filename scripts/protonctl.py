#!/usr/bin/env python3
"""Control ProtonNode through its private tailnet API. Uses only Python's stdlib."""
import argparse
import json
import os
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request


def selection(country: str | None, mode: str, hostname: str | None, city: str | None = None) -> dict:
    # Explicitly clear previous filters when moving to another country or mode.
    # false disables a required-feature filter; it does not exclude that feature.
    return {"provider": {"server_selection": {
        "countries": [country] if country else [],
        "hostnames": [hostname] if hostname else [],
        "regions": [], "cities": [city] if city else [], "names": [], "numbers": [], "categories": [], "isps": [],
        "free_only": False, "stream_only": False, "secure_core_only": False,
        "tor_only": mode == "tor", "port_forward_only": mode == "p2p",
    }}}


def request(base: str, key: str, method: str, path: str, body: dict | None = None) -> str:
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("URL must be an http(s) base URL without credentials, query, or fragment")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"X-API-Key": key, "Content-Type": "application/json",
                                          "X-ProtonNode-Request": "1"})
    # Never pass the tailnet control credentials through HTTP_PROXY/HTTPS_PROXY.
    # Refuse redirects so a redirected request cannot forward the API key.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=60) as response:
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("Controller response exceeded 2 MB")
        return data.decode()


def switch_server(base: str, key: str, settings: dict) -> str:
    """Drain the old VPN before applying settings; Tailscale stays connected."""
    was_running = json.loads(request(base, key, "GET", "/v1/vpn/status")).get("status") == "running"
    try:
        request(base, key, "PUT", "/v1/vpn/status", {"status": "stopped"})
        deadline = time.monotonic() + 20
        while json.loads(request(base, key, "GET", "/v1/vpn/status")).get("status") != "stopped":
            if time.monotonic() >= deadline:
                raise TimeoutError("VPN did not stop before changing servers")
            time.sleep(0.2)
        # Gluetun acknowledges stop before every health-check cancellation drains.
        # Applying settings during that cancellation can leave its checker stuck.
        time.sleep(1)
        request(base, key, "PUT", "/v1/vpn/settings", settings)
        # Identical settings do not auto-start a stopped tunnel.
        return request(base, key, "PUT", "/v1/vpn/status", {"status": "running"})
    except Exception:
        if was_running:
            try:
                request(base, key, "PUT", "/v1/vpn/status", {"status": "running"})
            except Exception:
                pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("PROTONNODE_URL", "http://proton-control:8000"))
    parser.add_argument("--key-file", type=Path, default=Path(__file__).resolve().parents[1] / "secrets/control.key")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("ip")
    commands.add_parser("start")
    commands.add_parser("stop")
    commands.add_parser("servers", help="list public WireGuard server metadata")
    switch = commands.add_parser("switch", help="change server, verify internet, and persist the selection")
    switch.add_argument("--country", help="Gluetun country name, e.g. Netherlands; omit to use any country")
    switch.add_argument("--city", help="Gluetun city name, e.g. Miami; optionally combine with --country")
    switch.add_argument("--hostname", help="exact Proton hostname from Gluetun's server list")
    switch.add_argument("--mode", choices=("p2p", "tor", "any"), default="p2p")
    args = parser.parse_args()
    try:
        # A device owned by an allowed Tailscale account needs no key file.
        key = args.key_file.read_text().strip() if args.key_file.exists() else ""
        if args.command == "switch":
            criteria = {k: v for k, v in {"country": args.country, "city": args.city,
                        "hostname": args.hostname, "mode": args.mode}.items() if v}
            result = submit_operation(args.url, key, {"action": "switch", "selection": criteria})
        elif args.command in ("start", "stop"):
            result = submit_operation(args.url, key, {"action": "connect" if args.command == "start" else "disconnect"})
        else:
            path = {"status": "/api/v1/status", "ip": "/v1/publicip/ip", "servers": "/api/v1/servers"}[args.command]
            result = request(args.url, key, "GET", path)
        print(result)
    except (OSError, ValueError, urllib.error.URLError) as error:
        parser.exit(1, f"Control request failed: {error}\n")


def submit_operation(base: str, key: str, command: dict, timeout: int = 300) -> str:
    operation = json.loads(request(base, key, "POST", "/api/v1/operations", command))
    deadline = time.monotonic() + timeout
    while operation["state"] not in ("succeeded", "failed", "interrupted"):
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Operation {operation['id']} is still running; check the dashboard")
        time.sleep(1)
        operation = json.loads(request(base, key, "GET", "/api/v1/operations/" + operation["id"]))
    if operation["state"] != "succeeded":
        raise ValueError(f"{operation.get('error') or operation['state']}; {operation['stage']}")
    return json.dumps(operation, indent=2)


if __name__ == "__main__":
    main()
