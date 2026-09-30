#!/bin/sh
# Restrict the dedicated worker UID without changing other users' firewall rules.
set -eu

agent_uid=$(id -u tracehunter-agent)
relay_ips=$(getent ahostsv4 super-relay.byted.org | awk '{print $1}' | sort -u)
proxy_ips=$(getent ahostsv4 sys-proxy-rd-relay.byted.org | awk '{print $1}' | sort -u)
test -n "$relay_ips"

install -d -m 0755 /etc/trace-hunter
hosts_stage=$(mktemp /etc/trace-hunter/.agent-hosts.XXXXXX)
nss_stage=$(mktemp /etc/trace-hunter/.agent-nsswitch.XXXXXX)
trap 'test ! -f "$hosts_stage" || rm "$hosts_stage"; test ! -f "$nss_stage" || rm "$nss_stage"' EXIT HUP INT TERM
printf '127.0.0.1 localhost\n::1 localhost\n' > "$hosts_stage"
for address in $relay_ips; do
    printf '%s super-relay.byted.org\n' "$address" >> "$hosts_stage"
done
for address in $proxy_ips; do
    printf '%s sys-proxy-rd-relay.byted.org\n' "$address" >> "$hosts_stage"
done
chmod 0644 "$hosts_stage"
mv "$hosts_stage" /etc/trace-hunter/agent-hosts
sed 's/^hosts:.*/hosts: files/' /etc/nsswitch.conf > "$nss_stage"
grep -q '^hosts: files$' "$nss_stage"
chmod 0644 "$nss_stage"
mv "$nss_stage" /etc/trace-hunter/agent-nsswitch

if ! iptables -n -L TH_AGENT_EGRESS >/dev/null 2>&1; then
    iptables -N TH_AGENT_EGRESS
fi
iptables -F TH_AGENT_EGRESS
iptables -A TH_AGENT_EGRESS -d 127.0.0.1/32 -p tcp --dport 8767 -j RETURN
# The auto-import worker talks only to Mulmo's loopback-bound native PTY bridge.
iptables -A TH_AGENT_EGRESS -d 127.0.0.1/32 -p tcp --dport 34567 -j RETURN
for address in $relay_ips; do
    iptables -A TH_AGENT_EGRESS -d "$address"/32 -p tcp --dport 443 -j RETURN
done
for address in $proxy_ips; do
    iptables -A TH_AGENT_EGRESS -d "$address"/32 -p tcp --dport 8118 -j RETURN
done
iptables -A TH_AGENT_EGRESS -j REJECT
if ! iptables -C OUTPUT -m owner --uid-owner "$agent_uid" -j TH_AGENT_EGRESS >/dev/null 2>&1; then
    iptables -I OUTPUT 1 -m owner --uid-owner "$agent_uid" -j TH_AGENT_EGRESS
fi

if ! ip6tables -n -L TH_AGENT_EGRESS6 >/dev/null 2>&1; then
    ip6tables -N TH_AGENT_EGRESS6
fi
ip6tables -F TH_AGENT_EGRESS6
ip6tables -A TH_AGENT_EGRESS6 -j REJECT
if ! ip6tables -C OUTPUT -m owner --uid-owner "$agent_uid" -j TH_AGENT_EGRESS6 >/dev/null 2>&1; then
    ip6tables -I OUTPUT 1 -m owner --uid-owner "$agent_uid" -j TH_AGENT_EGRESS6
fi
