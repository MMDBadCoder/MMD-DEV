#!/usr/bin/env bash
# Workspace network isolation. This is the rule set that enforces
# "a developer must not be able to reach out and touch the host".
set -euo pipefail
cd "$(dirname "$0")" && . ./config.sh
need_root

# Clean up what Docker left behind.
#
# This matters far more than it looks. Purging the Docker packages does NOT
# remove the nftables rules already loaded in the kernel, and Docker's FORWARD
# chain in `table ip filter` has **policy drop** while accepting only its own
# docker0 traffic. It sits on the same forward hook as Incus's rules, so every
# workspace loses all outbound connectivity - apt and Docker Hub both dead -
# with no error anywhere pointing at Docker. Verified: deleting these tables
# restored egress immediately.
#
# Only delete them once Docker is genuinely gone and no other firewall manages
# iptables, or this would tear down rules something else depends on.
if ip link show docker0 >/dev/null 2>&1; then
  log "removing leftover docker0 bridge"
  ip link set docker0 down 2>/dev/null || true
  ip link delete docker0 type bridge 2>/dev/null || true
fi

if ! command -v dockerd >/dev/null 2>&1 && ! systemctl is-active --quiet ufw; then
  for t in "ip filter" "ip nat" "ip raw" "ip6 filter" "ip6 nat"; do
    if nft list table $t >/dev/null 2>&1 && nft list table $t | grep -q DOCKER; then
      log "deleting Docker's leftover nftables table: $t"
      nft delete table $t || true
    fi
  done
fi

# The uplink's own subnet is the provider's LAN - other customers' machines
# live there. Tenants must not be able to reach it.
UPLINK_IF=$(ip -o route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
UPLINK_NET=$(ip -o -f inet addr show "$UPLINK_IF" | awk '{print $4}' | head -1)
UPLINK_NET=$(python3 -c "import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))" "$UPLINK_NET")
log "uplink ${UPLINK_IF} on ${UPLINK_NET} - will be blocked from workspaces"

RULES=/etc/nftables/mmd-isolation.nft
mkdir -p /etc/nftables

cat > "$RULES" <<NFT
#!/usr/sbin/nft -f
# MMD-DEV workspace isolation.
#
# Deliberately DROP-ONLY, at priority -10 (ahead of Incus's own tables at 0).
# nftables evaluates every base chain registered on a hook in priority order;
# a 'drop' is terminal across the whole ruleset, while falling off the end of
# this chain lets Incus's normal accept/NAT rules run untouched. So this table
# subtracts reachability and never grants it.
#
# NB: no 'flush ruleset' here - that would wipe the tables Incus manages.
# Create-then-delete is the idempotent way to replace just our own table.

table inet mmd_isolation
delete table inet mmd_isolation

table inet mmd_isolation {
    # Everything a workspace must never reach, in one place.
    set blocked_dest {
        type ipv4_addr
        flags interval
        elements = {
            169.254.0.0/16,      # link-local, incl. 169.254.169.254 cloud
                                 # metadata - a direct path to the provider's
                                 # API credentials. The single most important
                                 # entry in this table.
            127.0.0.0/8,         # host loopback (Incus API lives here)
            10.0.0.0/8,
            172.16.0.0/12,
            192.168.0.0/16,
            100.64.0.0/10,       # CGNAT
            ${UPLINK_NET},       # the provider's LAN: neighbouring machines
        }
    }

    # Container -> this host.
    chain input {
        type filter hook input priority -10; policy accept;

        iifname != "${INCUS_BRIDGE}" accept

        # The only host services a workspace legitimately needs are the
        # bridge's own DNS and DHCP (Incus's dnsmasq).
        udp dport 67 accept                                  # DHCP is broadcast
        ip daddr ${INCUS_BRIDGE_IP} udp dport 53 accept
        ip daddr ${INCUS_BRIDGE_IP} tcp dport 53 accept
        ip daddr ${INCUS_BRIDGE_IP} icmp type echo-request accept

        # Everything else from a workspace to this host is denied: sshd, the
        # dashboard on 443, Postgres, and the Incus API on 8443/9101.
        log prefix "mmd-drop-input " level info limit rate 5/minute
        drop
    }

    # Container -> elsewhere (routed).
    chain forward {
        type filter hook forward priority -10; policy accept;

        iifname != "${INCUS_BRIDGE}" accept

        # Tenant-to-tenant. Workspaces share a bridge but must not see
        # each other.
        oifname "${INCUS_BRIDGE}" log prefix "mmd-drop-tenant " level info limit rate 5/minute
        oifname "${INCUS_BRIDGE}" drop

        # Private ranges and metadata. Outbound internet falls through to
        # Incus's accept + masquerade, so apt and Docker Hub still work.
        ip daddr @blocked_dest log prefix "mmd-drop-private " level info limit rate 5/minute
        ip daddr @blocked_dest drop
    }

    # No IPv6 is issued to workspaces (ipv6.address=none), but deny by
    # default in case that ever changes.
    chain forward6 {
        type filter hook forward priority -10; policy accept;
        iifname "${INCUS_BRIDGE}" ip6 daddr ::/0 drop
    }
}
NFT

log "applying $RULES"
nft -f "$RULES"

# Apply at boot, after Incus has built its own tables.
cat > /etc/systemd/system/mmd-isolation.service <<UNIT
[Unit]
Description=MMD-DEV workspace network isolation
After=nftables.service incus.service
Wants=nftables.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f ${RULES}
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable mmd-isolation.service >/dev/null 2>&1

log "active rules:"
nft list table inet mmd_isolation | sed 's/^/    /'
log "done - next: 40-swap-zram.sh"
