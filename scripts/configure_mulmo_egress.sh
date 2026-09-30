#!/bin/sh
# Restrict MulmoTerminal's dedicated UID without changing the Agent worker rules.
set -eu

terminal_uid=$(id -u tracehunter-terminal)
relay_ips=$(getent ahostsv4 super-relay.byted.org | awk '{print $1}' | sort -u)
proxy_ips=$(getent ahostsv4 sys-proxy-rd-relay.byted.org | awk '{print $1}' | sort -u)
test -n "$relay_ips"

if ! iptables -n -L TH_MULMO_EGRESS >/dev/null 2>&1; then
    iptables -N TH_MULMO_EGRESS
fi
iptables -F TH_MULMO_EGRESS
# Replies to an established SSH-forwarded browser connection target an ephemeral port.
iptables -A TH_MULMO_EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
iptables -A TH_MULMO_EGRESS -d 127.0.0.1/32 -p tcp --dport 34567 -j RETURN
iptables -A TH_MULMO_EGRESS -d 127.0.0.1/32 -p tcp --dport 8767 -j RETURN
for address in $relay_ips; do
    iptables -A TH_MULMO_EGRESS -d "$address"/32 -p tcp --dport 443 -j RETURN
done
for address in $proxy_ips; do
    iptables -A TH_MULMO_EGRESS -d "$address"/32 -p tcp --dport 8118 -j RETURN
done
iptables -A TH_MULMO_EGRESS -j REJECT
if ! iptables -C OUTPUT -m owner --uid-owner "$terminal_uid" -j TH_MULMO_EGRESS >/dev/null 2>&1; then
    iptables -I OUTPUT 1 -m owner --uid-owner "$terminal_uid" -j TH_MULMO_EGRESS
fi

if ! ip6tables -n -L TH_MULMO_EGRESS6 >/dev/null 2>&1; then
    ip6tables -N TH_MULMO_EGRESS6
fi
ip6tables -F TH_MULMO_EGRESS6
ip6tables -A TH_MULMO_EGRESS6 -j REJECT
if ! ip6tables -C OUTPUT -m owner --uid-owner "$terminal_uid" -j TH_MULMO_EGRESS6 >/dev/null 2>&1; then
    ip6tables -I OUTPUT 1 -m owner --uid-owner "$terminal_uid" -j TH_MULMO_EGRESS6
fi
