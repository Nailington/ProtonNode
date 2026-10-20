#!/bin/sh
set -eu

: "${VPN_GATEWAY_IP:?Set the VPN container's private transit address}"
: "${EXIT_TRANSIT_IP:?Set the exit container's private transit address}"
: "${PROTON_DNS_ADDRESS:=10.2.0.1}"
iptables -w -P FORWARD DROP
ip6tables -w -P FORWARD DROP

transit_interface=$(ip -4 route get "$VPN_GATEWAY_IP" | sed -n 's/.* dev \([^ ]*\).*/\1/p' | head -n 1)
test -n "$transit_interface"
ip link del pn-data 2>/dev/null || true
ip tunnel add pn-data mode ipip local "$EXIT_TRANSIT_IP" remote "$VPN_GATEWAY_IP"
ip address add 169.254.222.2/30 dev pn-data

# Run before tailscaled creates tailscale0 or advertises an exit route. No
# forwarding rule permits the normal transport interface, even on routing loss.
iptables -w -N PN-EXIT 2>/dev/null || true
iptables -w -F PN-EXIT
iptables -w -A PN-EXIT -i tailscale0 -o pn-data -j ACCEPT
iptables -w -A PN-EXIT -i pn-data -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -w -A PN-EXIT -j DROP
iptables -w -C FORWARD -j PN-EXIT 2>/dev/null || iptables -w -I FORWARD 1 -j PN-EXIT
# Tailscale can retain netfilter=on in an existing identity and prepend its own
# filter ACCEPT rules while booting. An earlier mangle hook keeps the VPN-only
# guard effective regardless of those rules or preference migration.
iptables -w -t mangle -N PN-EXIT-GUARD 2>/dev/null || true
iptables -w -t mangle -F PN-EXIT-GUARD
iptables -w -t mangle -A PN-EXIT-GUARD -i tailscale0 -o pn-data -j ACCEPT
iptables -w -t mangle -A PN-EXIT-GUARD -i pn-data -o tailscale0 -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -w -t mangle -A PN-EXIT-GUARD -j DROP
iptables -w -t mangle -C FORWARD -j PN-EXIT-GUARD 2>/dev/null || iptables -w -t mangle -I FORWARD 1 -j PN-EXIT-GUARD
ip6tables -w -t mangle -C FORWARD -j DROP 2>/dev/null || ip6tables -w -t mangle -I FORWARD 1 -j DROP
iptables -w -t nat -C POSTROUTING -o pn-data -j SNAT --to-source 169.254.222.2 2>/dev/null ||
    iptables -w -t nat -A POSTROUTING -o pn-data -j SNAT --to-source 169.254.222.2
ip link set pn-data up

# This rule precedes Tailscale's own policy rules. Only packets arriving from
# tailscale0 use the VPN table. The daemon's UDP/DERP transport uses the ordinary
# Docker uplink. An unreachable default prevents fallback if the VPN route goes.
ip -4 route replace unreachable default metric 32767 table 100
ip -4 route replace default via 169.254.222.1 dev pn-data metric 10 table 100
ip -4 rule show | grep -q '^1000:.*iif tailscale0.*lookup 100$' ||
    ip -4 rule add priority 1000 iif tailscale0 lookup 100
ip -6 route replace unreachable default table 100
ip -6 rule show | grep -q '^1000:.*iif tailscale0.*lookup 100$' ||
    ip -6 rule add priority 1000 iif tailscale0 lookup 100

# Exit DNS is handled by tailscaled using this container's resolver. Pin that
# resolver to Proton and prevent TCP/UDP DNS from taking the transport uplink.
ip -4 route replace "$PROTON_DNS_ADDRESS/32" via 169.254.222.1 dev pn-data
iptables -w -N PN-DNS 2>/dev/null || true
iptables -w -F PN-DNS
iptables -w -A PN-DNS -d "$PROTON_DNS_ADDRESS/32" -o pn-data -j RETURN
iptables -w -A PN-DNS -j REJECT
for protocol in tcp udp; do
    iptables -w -C OUTPUT -p "$protocol" --dport 53 -j PN-DNS 2>/dev/null ||
        iptables -w -A OUTPUT -p "$protocol" --dport 53 -j PN-DNS
    ip6tables -w -C OUTPUT -p "$protocol" --dport 53 -j REJECT 2>/dev/null ||
        ip6tables -w -A OUTPUT -p "$protocol" --dport 53 -j REJECT
done

echo "protonnode: kernel exit routing ready via $VPN_GATEWAY_IP on $transit_interface"
/usr/local/bin/containerboot "$@" &
boot_pid=$!
trap 'kill -TERM "$boot_pid" 2>/dev/null || true; wait "$boot_pid" || true; exit 0' INT TERM
# TS_AUTH_ONCE retains identity but can skip TS_EXTRA_ARGS on existing logins.
# Apply our preferences explicitly, including when migrating a userspace identity.
until tailscale --socket="${TS_SOCKET:-/var/run/tailscale/tailscaled.sock}" set \
    --netfilter-mode=off --advertise-exit-node --accept-routes=false --accept-dns=false; do
    if ! kill -0 "$boot_pid" 2>/dev/null; then
        wait "$boot_pid"
        exit 1
    fi
    sleep 1
done
wait "$boot_pid"
