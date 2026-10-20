#!/usr/bin/env python3
"""Import an exported Proton WireGuard file without printing its private key."""
import argparse
import base64
import configparser
import ipaddress
import os
from pathlib import Path
import re
import tempfile


def import_config(source: Path, project: Path) -> None:
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(source.read_text())
    interface = config["Interface"]
    key = interface["PrivateKey"].strip()
    if len(base64.b64decode(key, validate=True)) != 32:
        raise ValueError("WireGuard PrivateKey must decode to 32 bytes")
    addresses = [str(ipaddress.ip_interface(v.strip())) for v in interface["Address"].split(",")
                 if ipaddress.ip_interface(v.strip()).version == 4]
    dns = [str(ipaddress.ip_address(v.strip())) for v in interface["DNS"].split(",")
           if ipaddress.ip_address(v.strip()).version == 4]
    if not addresses or not dns:
        raise ValueError("This IPv4 gateway requires an IPv4 Address and DNS in the export")
    env = project / ".env"
    if env.exists():
        text = env.read_text()
    else:
        text = (project / ".env.example").read_text()
        text = text.replace("replace-with-exit-node-auth-key", "").replace("replace-with-control-node-auth-key", "")
    for name, value in {"WIREGUARD_PRIVATE_KEY": key, "WIREGUARD_ADDRESSES": ",".join(addresses),
                        "PROTON_DNS_ADDRESS": dns[0]}.items():
        pattern = rf"^{name}=.*$"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, lambda _: f"{name}={value}", text, flags=re.MULTILINE)
        else:
            text += f"\n{name}={value}\n"
    with tempfile.NamedTemporaryFile(mode="w", dir=project, delete=False) as temporary:
        temporary.write(text)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, env)
    env.chmod(0o600)
    (project / "config/exit-resolv.conf").write_text(f"nameserver {dns[0]}\noptions timeout:2 attempts:2\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        import_config(args.config, args.project)
    except (OSError, ValueError, KeyError, configparser.Error):
        parser.exit(1, "Import failed. Check the file has valid Interface PrivateKey, Address, and DNS fields.\n")
    print("Imported Proton IPv4 settings into private .env. Recreate the containers to apply them.")
