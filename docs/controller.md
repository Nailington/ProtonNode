# Private controller deployment

Run this project on a **Linux VPS with rootful Docker and Compose 2.33.1+**.
Nothing depends on QEMU, the test VM's address, or an SSH tunnel. The VPS can
already run Tailscale and other applications: the two containerized Tailscale
nodes have independent identities, namespace routing, and state volumes.
Follow the kernel-module and credential setup in [README](../README.md).

## Daily use

1. Connect your device to Tailscale and select `protonnode` as its exit node.
2. Open `http://proton-control:8000` (or the management node's Tailscale IP/FQDN).
3. Search for a city/server, choose P2P/Tor/Any, select a row, and press Connect.
4. The operation continues if you close the browser. Return to see its outcome.

The owner of an untagged management node is authorized automatically through
Tailscale WhoIs. To use a tagged management node or permit other administrators,
set `CONTROLLER_ALLOWED_LOGINS=you@example.com,other@example.com` in `.env` and run
`docker compose up -d control-proxy`. This **replaces** the owner default.
Tagged calling devices use the API key. Existing Tailscale policy must allow
administrators TCP 8000 on the management identity. Use a dedicated tag/grant for
that identity if desired. The application does not change your tailnet policy.

Set `CONTROLLER_ALLOWED_LOGINS=*` to allow all Tailscale-verified devices,
including tagged devices, to read and control the panel. In this mode, manage
access with Tailscale ACLs/grants for TCP port 8000 on `proton-control`.
Explicit invalid API keys are still rejected. Recreate `control-proxy` after changing this setting.

A holder of the control key can manage the VPN regardless of account-owner rules,
but must still be able to reach the management node through Tailscale policy.
The same key authenticates the internal Gluetun/probe APIs; keep it private.
The socket mount is for local Tailscale identity reads. The app deliberately
exposes no generic LocalAPI, Docker, Gluetun settings, or shell proxy.

The dashboard is reachable independently of the exit route, including when the
VPN is disconnected or unhealthy. If the entire VPN container was explicitly
stopped with Docker, run `docker compose up -d gluetun` on the VPS to start it.
The controller intentionally cannot operate Docker. Its bounded stuck-tunnel
recovery asks the authenticated probe inside Gluetun to terminate that container's
PID 1, which Docker's `unless-stopped` policy restarts.

## API contract

Use JSON requests with `Content-Type: application/json` and
`X-ProtonNode-Request: 1`. Authenticate with `Authorization: Bearer <control-key>`
or `X-API-Key: <control-key>`, or use an authorized Tailscale identity directly.
Never put secrets in query parameters. Requests from another web origin are
rejected; no cross-origin browser API is enabled.

`POST /api/v1/operations` accepts these bodies:

```json
{"action":"switch","selection":{"country":"United States","city":"Miami","mode":"p2p"}}
```

```json
{"action":"switch","selection":{"hostname":"node-us-446.protonvpn.net","mode":"p2p"}}
```

```json
{"action":"disconnect"}
```

Use `connect` to resume the saved server, or `reconnect` to drain/restart it.
Use `speedtest` to measure raw Proton on the gateway.
A switch is also allowed while disconnected. Modes are `p2p`, `tor`, and `any`.
All filters must match; hostname can be combined with country/city.
Hostname examples depend on the current catalog; obtain real entries from
`GET /api/v1/servers`. It returns public ordinary WireGuard server metadata,
with `p2p` and `tor` booleans. Secure Core is not an offered route type.
Capabilities indicate catalog support, not current server load or reachability.
P2P capability does not configure an inbound forwarded port.

The response is HTTP 202, with `id`, `state`, `stage`, `request`, timestamps,
and optional `result`/`error`. Poll `GET /api/v1/operations/<id>`.
`queued` and `running` are nonterminal. `succeeded`, `failed`, and `interrupted`
are terminal. A failed switch includes `result.rollback` of `succeeded`, `failed`,
or `unavailable`; inspect it rather than assuming recovery.
`GET /api/v1/operations` lists the 20 most recent operations.
`GET /api/v1/status` includes current connection, actual endpoint/public IP,
check timestamp, saved selection, and recent operations.

Supply an optional unique `Idempotency-Key` header (1–100 ASCII letters, digits,
underscore, dot, colon, or hyphen). Retry the **same body with the same key** to
retrieve the original operation rather than repeating it. A different body with
that key returns 409. Another operation while busy returns 409; invalid input
returns 400; invalid credentials return 401; cross-origin/Host failures return
403. During startup recovery, writes return 503. Request bodies are capped at
8 KiB. Normal public-IP checks are cached up to 45 seconds; switch verification
always requests a fresh check. This checks the gateway's internet path; it does
not replace client-side DNS/IPv6 checks.

## On-demand throughput measurement

`POST /api/v1/operations` with `{"action":"speedtest"}` starts the raw
Proton measurement on the gateway. It uses the normal operation ID, polling,
idempotency, authentication, and serialization rules. GET/status reads never
start a measurement. Startup and reconnect recovery do not replay interrupted
tests. No arbitrary target URL, transfer size, or shell command is accepted.

The operation performs three 50 MB HTTPS downloads and three 20 MB uploads to
Cloudflare, through the existing VPN namespace. Results include
`download_mbps`, `upload_mbps` sample arrays, `download_median_mbps`,
`upload_median_mbps`, `server`, `endpoint`, `public_ip`, `measured_at`, and
transfer sizes. It verifies the same Proton endpoint/IP before and after the
run. Tests have a bounded duration and a 30-second cooldown. Failure records a
failed operation without reconnecting, rolling back, or changing the selection.
`GET /api/v1/status` includes the most recent successful result in
`last_speedtest`, even after it leaves the 20-entry activity list.

Speeds may vary when connected through Tailscale. Only raw Proton is measured;
the test never changes management routing. It transfers up to 210 MB on the server.
The browser makes only same-origin API requests.

## Restart and migration

State volumes are project-prefixed by Compose:

- `exit-state` and `control-state`: private Tailscale identity state.
- `controller-state`: selection.json plus SQLite operation history and WAL files.
- `gluetun-state`: Gluetun catalog/data.
- `control-runtime`: ephemeral daemon socket; do not migrate its contents.

Back up `.env`, `secrets/`, and the identity/controller volumes privately. Stop the
stack before filesystem-level volume backups so SQLite and identity files are
consistent. Do not run a copied Tailscale identity on two machines at once.
For a new VPS, the simplest migration is a fresh checkout plus new Tailscale
nodes/auth keys and your Proton WireGuard key. Reapprove the exit node, select
it on your devices, and choose the desired server in the dashboard. Preserve the
old deployment until the new one is verified, then stop it. If retaining existing
node identities instead, stop the old stack before restoring its volumes to the
new host under the same Compose project name.

The `.env` server filters are initial defaults. A verified saved selection takes
precedence on later restarts. A disconnected saved state keeps `pn-data` down at
boot and the controller stops the VPN once its API is available. Interrupted
operations are marked as such, and the last committed selection is reconciled.
Connection changes are serialized; they never edit `.env` or private keys.

For an ordinary upgrade:

```sh
docker compose config --quiet
docker compose up -d --build
```

Avoid `docker compose down -v`: it deletes persistent state, including identities.
Docker namespace capabilities, tun access, kernel IPIP/nftables support, and a
non-overlapping transit subnet are the same requirements on the VPS as in the VM.

## Recovery if a saved server has been retired

If the gateway is running, choose another catalog server in the dashboard.
If a retired selection prevents Gluetun from starting, stop the stack, remove
only `selection.json` from the controller volume, then start with your `.env`
filters. This resets the saved server; identity volumes and history stay intact:

```sh
docker compose stop
# This image already exists after a normal deployment; it contains no VPN secrets.
docker compose run --rm --no-deps --entrypoint python3 control-proxy -c \
  "from pathlib import Path; Path('/var/lib/protonnode/selection.json').unlink(missing_ok=True)"
docker compose up -d
```

The management controller never reads your Proton private key. The read-only
state mount in Gluetun carries selection metadata only. All forwarding protections
remain in place during changes and recovery; an unavailable VPN is not permission
to forward traffic through the VPS uplink.

## Development preview

No external Python packages are needed:

```sh
python3 controller/app.py --demo --port 8080 --state-dir .local/controller-preview
```

This binds **127.0.0.1 only** and uses synthetic server/IP data with a visible
preview banner. It cannot control a VPN. Production has no demo flag and binds
only the authenticated management node's Tailscale IPs. The browser UI has no
build step, CDN, or external font dependency.
