# ProtonNode

A containerized Proton VPN exit node for an existing Tailscale tailnet, designed
for a Linux VPS that also hosts other services.

**Status: early release, validated with a live Proton connection and real
Tailscale clients.** Validation covered routing, DNS, failure recovery, server
switching, access controls, and restart persistence. Complete the verification
below on your VPS and client devices before relying on the deployment.

The distribution contains the Docker stack, private dashboard/API, configuration
examples, and setup/control helpers. Test suites, VM tooling, speed-test scripts,
local credentials, and lab artifacts are kept outside the published files.

## What runs

- **`gluetun`** connects to Proton using WireGuard and manages its VPN firewall.
  Its image adds nftables for an independent forwarding guard and a private egress probe.
- **`tailscale-exit`** advertises `protonnode` from its own network namespace,
  using kernel routing. Its encrypted Tailscale transport uses an ordinary Docker
  uplink, preserving direct local/remote peer connections. Only forwarded exit
  traffic and Proton DNS use the private link to Gluetun.
- **`tailscale-control`** joins as a second node, `proton-control`, using ordinary
  VPS internet connectivity so management is independent of the Proton tunnel.
- **`control-proxy`** serves the dashboard and durable control API on the management
  node’s Tailscale addresses, port 8000. It verifies each connection before saving it.
  The service name is retained for compatibility; it no longer runs socat.

The VPS's existing Tailscale daemon keeps its own identity. These containers use
separate state volumes, no host networking, no published ports, and no Docker
socket mounts. Gluetun and both Tailscale nodes get `NET_ADMIN` and `/dev/net/tun`, scoped
to their own namespaces. The controller has no network-administration capability. Forwarding sysctls apply inside those namespaces; Docker
still creates its usual host bridge/NAT rules.

The two data containers share an internal Docker transit network with static
addresses configured in `.env`. An IPv4-in-IPv4 link (`pn-data`) carries exit
packets across it; its `169.254.222.0/30` addresses exist only inside the containers.
The gateway installs its guard before enabling this link, blocking client traffic
even during initial startup. Policy routing selects this path for packets arriving
from `tailscale0`, with an unreachable fallback route. Separate forwarding filters
allow only that path and the VPN interface. Gluetun's independent nftables guard
continues blocking ordinary-uplink bypass if Gluetun removes its own legacy
iptables rules during shutdown. Neither container uses host networking or host
IP-forwarding settings.

On your phone or computer, select **protonnode** in the existing Tailscale exit-node
list. The Proton app is unnecessary on those clients. Normal tailnet peer access
continues subject to your existing tailnet policy. Local Wi-Fi/LAN access is a
separate Tailscale setting; enable Allow LAN access if needed.

Tailscale supports exit-node clients on Windows, macOS, Linux, Android, iOS/iPadOS,
and tvOS. This starter deliberately blocks public IPv6 forwarding; IPv6 internet
destinations should fail, never use the VPS uplink. Tailnet IPv6 peer access remains
available. The gateway runs on Linux; client devices use their usual Tailscale app.
See [exit nodes](https://tailscale.com/docs/features/exit-nodes) and
[kernel routing](https://tailscale.com/docs/reference/kernel-vs-userspace-routers).

## Setup on the VPS

Requires rootful Docker Engine with Compose **2.33.1+**, `/dev/net/tun`, Linux IPIP
and IPv6 firewall/nftables support, and Python 3.11+ for the helper scripts. Use
this directory on the VPS, not your client device. Load any modular kernel
features on the Docker host before starting; containers do not get `SYS_MODULE`:

```sh
sudo modprobe ipip
sudo modprobe iptable_mangle
sudo modprobe ip6table_filter
sudo modprobe ip6table_mangle
sudo modprobe nf_tables
```

For hosts with systemd, load them at boot as well:

```sh
printf '%s\n' ipip iptable_mangle ip6table_filter ip6table_mangle nf_tables | sudo tee /etc/modules-load.d/protonnode.conf
```

The Proton tunnel defaults to `WIREGUARD_MTU=1400`, adjustable in `.env`. Tailscale's
encrypted packets now travel outside Proton, avoiding the nested packet-size and
internet-detour problems in the earlier shared-namespace design. Client throughput
still depends on the client-to-gateway path, gateway resources, and Proton's path.

1. In your Proton account, generate a Linux WireGuard configuration. Copy its
   `PrivateKey`, `Address`, and `DNS` values into the settings below. Do not use
   your account password. Gluetun's built-in Proton provider selects the server
   from its own catalog, so you do not copy `Endpoint` or `PublicKey`.
   See [Proton configuration export](https://protonvpn.com/support/wireguard-configurations)
   and [Gluetun's Proton provider](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/protonvpn.md).
2. In Tailscale, create two non-ephemeral, preauthorized auth keys for the new exit
   and control identities. If using tagged keys, assign the intended tags and
   permissions in your policy. Each node gets a separate persistent state volume.
3. Prepare the local files:

   ```sh
   cp .env.example .env
   chmod 600 .env
   python3 scripts/init-secrets.py
   ```

4. Edit `.env` with those keys and your Proton settings. The startup default is a
   Netherlands P2P-capable server. If the Proton DNS address differs from
   `10.2.0.1`, also edit `config/exit-resolv.conf`. DNS goes to Proton inside the
   tunnel, which is particularly relevant for `.onion` resolution on Tor servers.
   If `172.30.222.0/29` overlaps an existing network, change `VPN_TRANSIT_SUBNET`,
   `VPN_GATEWAY_IP`, and `EXIT_TRANSIT_IP` together before starting. Separate
   deployments must use distinct transit subnets as well as Compose project names.
5. Start the containers:

   ```sh
   docker compose config --quiet
   docker compose up -d --build
   docker compose logs --tail=80 gluetun tailscale-exit tailscale-control
   ```

6. Approve `protonnode` as an exit node in the Tailscale admin console. Approve
   either new device if your tailnet requires device approval. If you have a
   custom access policy, allow the intended clients access to `autogroup:internet`
   and allow administrators TCP port 8000 on `proton-control`. Access policies are
   additive: an existing allow-all rule must be narrowed if management access
   should be restricted to particular users.
7. Select `protonnode` on a test client. Keep its Tailscale DNS settings enabled
   and verify the actual public IP, DNS behavior, and tailnet access below.

After both nodes enroll successfully, you can clear `TS_EXIT_AUTHKEY` and
`TS_CONTROL_AUTHKEY` from `.env`. `TS_AUTH_ONCE=true` and the state volumes retain
their enrollment. Preserve the volumes; `docker compose down -v` deletes identity
state. Manage node key expiry for an unattended gateway in Tailscale.

Both Tailscale device names are configurable in `.env`: `EXIT_HOSTNAME=protonnode`
names the exit node, and `CONTROL_HOSTNAME=proton-control` names the dashboard node.
After changing either value, run `docker compose up -d` to recreate the affected
containers. Use the new control hostname in the dashboard URL. Tailscale may add
a suffix if another device already uses the requested name.

## Private dashboard and API

Open **[http://proton-control:8000](http://proton-control:8000)** from a tailnet device.
Use the full MagicDNS name or its Tailscale IP if short names do not resolve.
The dashboard supports mobile layouts, country/search filters, P2P/Tor choices,
favorites, connection progress, and recent operations. It automatically recognizes
the management node’s Tailscale owner. For a tagged management node, or access by
additional people, set `CONTROLLER_ALLOWED_LOGINS` in `.env` to the allowed Tailscale
login emails, separated by commas, then recreate `control-proxy`. Tagged caller
devices require API-key authentication. Tailnet ACLs/grants still apply.

The listener binds only the management node’s Tailscale IPv4/IPv6 addresses.
There are no published ports, host-network dependencies, external assets, or Funnel
exposure. HTTP travels inside Tailscale’s encrypted connection. The native Gluetun
API and probe remain on the private Docker bridge. Do not publish them through a
reverse proxy. The dashboard rejects cross-origin writes and unexpected Host names.

Set `CONTROLLER_ALLOWED_LOGINS=*` to allow all Tailscale-verified devices,
including tagged devices, to read and control the panel. In this mode, manage
access with Tailscale ACLs/grants for TCP port 8000 on `proton-control`.
Explicit invalid API keys are still rejected. Recreate `control-proxy` after changing this setting.

The control key in `secrets/control.key` also works in the dashboard or scripts.
The browser keeps an entered key only in memory for that tab. Favorites contain
only public hostnames and stay in that browser. Never put a key in a URL.
The stdlib CLI uses your Tailscale identity when no local key file exists:

```sh
python3 scripts/protonctl.py status
python3 scripts/protonctl.py servers
python3 scripts/protonctl.py switch --country "United States" --city Miami --mode p2p
python3 scripts/protonctl.py switch --country Switzerland --mode tor
python3 scripts/protonctl.py stop
python3 scripts/protonctl.py start
```

Place `--url http://proton-control.YOUR-TAILNET.ts.net:8000` and/or
`--key-file /path/to/control.key` before the subcommand. `PROTONNODE_URL` is an
alternative to `--url`. Scripts ignore HTTP proxy variables and refuse redirects.

For automation, submit a typed operation rather than raw Gluetun settings:

```sh
curl http://proton-control:8000/api/v1/operations \
  -H 'Authorization: Bearer YOUR_CONTROL_KEY' \
  -H 'Content-Type: application/json' \
  -H 'X-ProtonNode-Request: 1' \
  -H 'Idempotency-Key: miami-switch-001' \
  --data '{"action":"switch","selection":{"country":"United States","city":"Miami","mode":"p2p"}}'
```

The response is `202` and includes an operation `id`. Poll
`GET /api/v1/operations/{id}` until `state` is `succeeded`, `failed`, or `interrupted`.
Reuse the same `Idempotency-Key` for a retry of the same body; a conflicting body
returns `409`. Only one change runs at a time. Other writes also return `409` while
busy; startup reconciliation returns `503`. Invalid selections return `400`
without disconnecting the working tunnel. Actions are `switch`, `connect`,
`disconnect`, `reconnect`, and `speedtest` (raw Proton only). Only `switch` accepts `selection`, with optional
`country`, `city`, `hostname`, and `mode` (`p2p`, `tor`, or `any`, default `p2p`).
Country/city pools resolve to an exact server before connecting. The public catalog
includes ordinary WireGuard servers; Secure Core is excluded.

Reads are `GET /api/v1/status`, `/api/v1/servers`, `/api/v1/operations`, and
`/api/v1/operations/{id}`. API-key authentication accepts `Authorization: Bearer`
or `X-API-Key`. Legacy **read** routes `/v1/vpn/status` and `/v1/publicip/ip` remain.
Raw Gluetun write routes are no longer exposed through the tailnet controller.
Its credential-bearing settings endpoint is never exposed.

A successful switch requires running VPN status, a healthy tunnel, the selected
WireGuard endpoint, and a fresh HTTPS public-IP request inside the VPN namespace.
Normal dashboard reads cache egress checks for up to 45 seconds and show the check
time. No invented load, latency, or availability metrics are shown. A failed switch
attempts to restore the last verified selection. If Gluetun’s health checker gets
stuck, recovery can restart just its container, without a Docker socket.
If recovery also fails, the operation reports that failure and the gateway’s
forwarding guard continues to block ordinary-uplink bypass.

Successful selections and operation history live in the `controller-state` volume.
The gateway reads that selection before starting, so it overrides the initial
`.env` filters after a restart. A deliberately disconnected state also keeps the
exit-data link down across gateway restarts. After a controller crash, unfinished
operations are marked interrupted and the saved state is restored. Preserve this
volume along with both Tailscale identity volumes. If a pinned server is retired,
choose another server; if Gluetun cannot start with that saved server, see the
recovery procedure in [deployment notes](docs/controller.md).

All devices using this exit node share its Proton server. A switch briefly drops
existing internet connections; tailnet access remains available. For simultaneous
P2P and Tor routes, deploy separate copies with distinct Compose project names,
Tailscale hostnames, transit subnets, and credentials. Watch your Proton concurrent
connection allowance.

The exact same stack runs on a Linux VPS already joined to Tailscale: its host
identity and other applications remain separate. See [deployment and recovery](docs/controller.md)
for migration, state backups, and private API examples.

## On-demand speed tests

Click **Run speed test** to measure the gateway’s raw Proton connection. Speeds
may vary when connected through Tailscale. The test runs on the gateway and
uses three 50 MB downloads and three 20 MB uploads to Cloudflare, reporting median
speeds. It transfers up to 210 MB on the server; the browser makes only same-origin
API requests. Results persist in operation history and are shared across viewers.

Tests require an explicit click/API operation and have a 30-second cooldown.
Nothing runs on refresh. Closing the panel does not cancel a running test.
The test verifies the Proton endpoint/IP before and after measurement and does
not change Tailscale routing or VPN settings.

## Tor and P2P expectations

Use P2P servers for torrents. Tor is a separate browsing mode: Tor carries TCP,
not general UDP traffic, and has substantially different performance. Tor over
VPN does not provide Tor Browser's browser fingerprint protections. Browser
settings and DNS can also affect `.onion` access. See
[Proton's Tor instructions](https://protonvpn.com/support/tor-vpn).

Tailscale's own transport stays outside Proton, including in Tor mode. Tor's TCP
limitations therefore apply to forwarded internet traffic without forcing the
Tailscale connection itself through Tor. The dashboard's live test switched to
`FR#13-TOR` and verified HTTPS egress, then restored Miami. Client browsing and
`.onion` DNS still need separate verification on your devices.

Outbound torrent traffic does not require port forwarding. Proton's optional
inbound port forwarding requires a P2P server, NAT-PMP enabled when exporting the
WireGuard key, and lease renewal. It is disabled here. Enabling
`VPN_PORT_FORWARDING=on` alone does **not** deliver the port to a torrent client
on your laptop: Proton forwards to the VPS tunnel, and an explicit relay to the
tailnet client would still be needed. Running a torrent client beside the VPN
would simplify inbound forwarding, but is outside this starter.
See [Proton's manual forwarding guide](https://protonvpn.com/support/port-forwarding-manual-setup).

## Live verification before use

Keep a separate admin session to the VPS while testing. No live privacy guarantees
can be established from Compose validation alone.

1. With the exit node selected on a client, check its public IPv4 address using
   `curl -4 https://ip.me` or a browser. It must be Proton's, not the VPS address
   or your client ISP's. Check again after a server change.
2. Check public IPv6 with `curl -6 --max-time 10 https://ip.me` or an IPv6 test.
   This starter is IPv4-only: the request must fail. A successful non-Proton
   address is a leak and must be fixed before use.
3. Resolve and connect to another device using its MagicDNS name and Tailscale IP.
   Also test any existing subnet-router destinations. Tailnet peer traffic should
   retain its normal Tailscale path, independent of the exit gateway.
4. Test DNS on the client, including any browser secure-DNS or OS private-DNS
   settings. Public queries should follow the intended VPN path; internal names
   should still resolve. Do not infer client DNS privacy solely from the resolver
   file inside the exit container.
5. Run `python3 scripts/protonctl.py stop` from the admin device. On the client,
   public internet requests must fail, while other tailnet devices and
   `proton-control:8000` should remain reachable. Then run the helper's `start`.
   Also test an unexpected tunnel/container failure and a VPS/container reboot.
   Verify requests never fall back to the VPS's ordinary public IP.
6. Check the VPS's existing services from their normal locations. Check the
   control endpoint is unreachable at `VPS_PUBLIC_IP:8000`. Verify an unauthorized Tailscale identity without a key is blocked, and an
   invalid explicit API key is rejected even for an authorized identity.
7. Test P2P on a P2P server. Separately test Tor browsing and `.onion` resolution
   on a Tor server, then repeat the checks on each client platform you use.

If the Gluetun process/container itself is stopped, the control node stays online
and the dashboard/history stay available, but connection changes cannot succeed
until Docker restarts Gluetun. API `disconnect` stops only the VPN tunnel.

The relevant packet paths, DNS settings, API authentication, and kill switch must
all pass these checks on your actual VPS.

Commands executed inside `tailscale-exit` itself use its ordinary transport
uplink, except for Proton DNS. Verify VPN egress from an actual client selecting
the exit node, or from `gluetun`; a local `wget` in the exit container does not
exercise the forwarded client path.

## Local checks

```sh
docker compose --env-file .env.example config --quiet
python3 -m py_compile controller/app.py config/vpn-probe.py scripts/*.py
node --check controller/static/app.js
```

Node.js is needed only for the optional JavaScript syntax check. These commands
validate configuration and source syntax; they do not establish that the remote
images start, the VPN connects, or traffic follows the intended route. Follow the
deployment verification above. Local test suites and VM tooling are not included
in the distribution.
