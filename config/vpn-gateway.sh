#!/bin/sh
set -eu

: "${EXIT_TRANSIT_IP:?Set the exit container's private transit address}"
: "${VPN_GATEWAY_IP:?Set the VPN container's private transit address}"
vpn_interface=${VPN_INTERFACE:-tun0}
selection=/run/protonnode-control/selection.json
# Saved, verified choices override .env on future container/host restarts.
# Never source or eval state: read only the specific fields we support.
if [ -f "$selection" ]; then
    jq -e '.version == 1 and (.desired == "running" or .desired == "stopped") and (.mode == "p2p" or .mode == "tor" or .mode == "any")' "$selection" >/dev/null
    SERVER_HOSTNAMES=$(jq -er '.server.hostname | select(type == "string" and test("^[a-zA-Z0-9.-]+$"))' "$selection")
    SERVER_NAMES=$(jq -er '.server.name | select(type == "string")' "$selection")
    SERVER_COUNTRIES=$(jq -r '.server.country' "$selection")
    SERVER_CITIES=$(jq -r '.server.city' "$selection")
    PORT_FORWARD_ONLY=$(jq -r 'if .mode == "p2p" then "on" else "off" end' "$selection")
    TOR_ONLY=$(jq -r 'if .mode == "tor" then "on" else "off" end' "$selection")
    export SERVER_HOSTNAMES SERVER_NAMES SERVER_COUNTRIES SERVER_CITIES PORT_FORWARD_ONLY TOR_ONLY
fi

# Client data arrives encapsulated over the internal bridge. A new namespace
# has no active IPIP receiver; bring ours up only after its guard exists. Thus
# clients cannot reach the ordinary uplink during firewall initialization.
iptables -w -P FORWARD DROP
ip link del pn-data 2>/dev/null || true
# Independent of Gluetun's legacy iptables: even if its graceful shutdown flushes
# its firewall or sets FORWARD ACCEPT, exit data may never use the normal uplink.
nft delete table inet protonnode 2>/dev/null || true
nft -f - <<EOF
table inet protonnode {
    chain forward {
        type filter hook forward priority -10; policy drop;
        iifname "pn-data" oifname "$vpn_interface" ip saddr 169.254.222.2 counter accept
        iifname "$vpn_interface" oifname "pn-data" ip daddr 169.254.222.2 ct state established,related counter accept
        counter drop
    }
}
EOF
ip tunnel add pn-data mode ipip local "$VPN_GATEWAY_IP" remote "$EXIT_TRANSIT_IP"
ip address add 169.254.222.1/30 dev pn-data
# Return packets must bypass Gluetun's catch-all WireGuard policy table.
ip -4 rule show | grep -q '^96:.*to 169.254.222.2.*lookup main$' ||
    ip -4 rule add priority 96 to 169.254.222.2/32 lookup main
ip -4 rule show | grep -q "^97:.*to $EXIT_TRANSIT_IP.*lookup main\$" ||
    ip -4 rule add priority 97 to "$EXIT_TRANSIT_IP/32" lookup main

configure() {
    iptables -w -N PN-VPN 2>/dev/null || true
    # These rules only ever allow the VPN interface, even while Gluetun restarts.
    iptables -w -C PN-VPN -i pn-data -s 169.254.222.2/32 -o "$vpn_interface" -j ACCEPT 2>/dev/null ||
        iptables -w -I PN-VPN 1 -i pn-data -s 169.254.222.2/32 -o "$vpn_interface" -j ACCEPT
    iptables -w -C PN-VPN -i "$vpn_interface" -o pn-data -d 169.254.222.2/32 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT 2>/dev/null ||
        iptables -w -I PN-VPN 2 -i "$vpn_interface" -o pn-data -d 169.254.222.2/32 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -w -C PN-VPN -j DROP 2>/dev/null || iptables -w -A PN-VPN -j DROP
    iptables -w -C FORWARD -j PN-VPN 2>/dev/null || iptables -w -I FORWARD 1 -j PN-VPN
    iptables -w -t nat -C POSTROUTING -s 169.254.222.2/32 -o "$vpn_interface" -j MASQUERADE 2>/dev/null ||
        iptables -w -t nat -A POSTROUTING -s 169.254.222.2/32 -o "$vpn_interface" -j MASQUERADE
}

configure
carrier() {
    if [ -f "$selection" ] && ! jq -e '.desired == "running"' "$selection" >/dev/null 2>&1; then
        ip link set pn-data down
    else
        ip link set pn-data up
    fi
}
carrier
python3 /usr/local/bin/protonnode-vpn-probe.py &
# Gluetun owns its firewall. Reconcile our narrow forwarding rules if it flushes
# them on a VPN restart; its default FORWARD DROP blocks data in the meantime.
(
    while sleep 2; do
        carrier
        if ! configure; then
            echo 'protonnode: VPN forwarding guard reconciliation failed; retrying' >&2
        fi
    done
) &
exec /gluetun-entrypoint "$@"
