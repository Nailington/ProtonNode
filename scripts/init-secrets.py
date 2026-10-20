#!/usr/bin/env python3
"""Generate the private control API credentials without printing the key."""
import argparse
import os
from pathlib import Path
import secrets


def initialize(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    key_path = directory / "control.key"
    if not key_path.exists():
        with open(key_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    key_path.chmod(0o600)
    key = key_path.read_text().strip()
    if len(key) < 32 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in key):
        raise ValueError("control.key must contain a URL-safe key of at least 32 characters")
    auth_path = directory / "control-auth.toml"
    if auth_path.exists():
        raise FileExistsError("control-auth.toml already exists; leaving existing credentials unchanged")
    with open(auth_path, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        f.write('[[roles]]\nname = "protonnode-admin"\n')
        f.write('routes = ["GET /v1/vpn/status", "PUT /v1/vpn/status", '
                '"PUT /v1/vpn/settings", "GET /v1/publicip/ip", "GET /v1/portforward"]\n')
        f.write(f'auth = "apikey"\napikey = "{key}"\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path(__file__).resolve().parents[1] / "secrets")
    args = parser.parse_args()
    try:
        initialize(args.directory)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Created API authentication files in {args.directory}. Keep them private.")
