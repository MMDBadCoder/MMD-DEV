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
# ...but only when Docker is genuinely GONE. This block exists for the bridge a
# PURGED Docker leaves behind, and it used to fire on the mere existence of
# docker0 - which on a host that legitimately runs Docker means tearing the
# bridge out from under running containers. Measured on this host: a StarRocks
# cluster, Grafana and Prometheus were up and healthy on Docker networks when
# this script would have deleted their bridge.
if ! command -v dockerd >/dev/null 2>&1 && ip link show docker0 >/dev/null 2>&1; then
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

# One accept per workspace allowed to reach this host's HTTPS, and nothing at
# all when the list is empty - the common case, and the one that must stay
# byte-for-byte the ruleset it was before this existed.
#
# The destination is the host's PUBLIC address, not the bridge address: a
# workspace resolves mmd-ai.ir through the normal DNS and gets that address, so
# permitting it here is what makes the ordinary public URL work from inside a
# machine, certificate and all. The packet still arrives on this hook because
# the address is local to the host.
UPLINK_IP=$(ip -o -f inet addr show "$UPLINK_IF" | awk '{print $4}' | head -1 | cut -d/ -f1)
HOST_HTTPS_RULES=""
for ws_ip in ${HOST_HTTPS_ALLOWED_WORKSPACES}; do
    HOST_HTTPS_RULES="${HOST_HTTPS_RULES}
        # Operator machine: HTTPS only, to a port the internet already reaches.
        ip saddr ${ws_ip} ip daddr ${UPLINK_IP} tcp dport 443 accept
        # And echo, because 'ping the address' is the first thing anyone tries
        # when a connection fails. Without it the diagnostic lies: ping reports
        # 100% loss whether the rule above is present or missing, which sends
        # the reader looking for a DNS or routing fault that is not there.
        ip saddr ${ws_ip} ip daddr ${UPLINK_IP} icmp type echo-request accept"
    log "workspace ${ws_ip} may reach ${UPLINK_IP}:443 and ping it (support agent MCP)"
done

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

table bridge mmd_bridge_isolation
delete table bridge mmd_bridge_isolation

# Tenant-to-tenant isolation, in the family that can actually see it.
#
# This exists because the equivalent rule in the inet forward chain does NOT
# work, which was measured rather than assumed: from one workspace, another
# workspace's sshd on port 22 and its Hermes dashboard on 9119 were both
# reachable, and ping succeeded, with `oifname incusbr0 drop` sitting right
# there in the ruleset.
#
# The reason is that two workspaces on the same bridge exchange BRIDGED frames.
# Those never traverse the ip/inet forward hook unless br_netfilter is loaded
# and bridge-nf-call-iptables is on - and it is not, deliberately: br_netfilter
# pushes every bridged frame through the IP hooks, which costs throughput and is
# a well-known source of breakage for Docker running inside the containers. The
# bridge family is where this traffic is visible, so this is where the rule
# belongs.
#
# Traffic between a workspace and the HOST is delivered locally to the bridge
# port rather than forwarded across it, so DHCP, DNS and the gateway are
# untouched by this. Outbound internet is routed, not bridged, so apt and
# Docker Hub are untouched too.
table bridge mmd_bridge_isolation {
    chain forward {
        type filter hook forward priority filter; policy accept;
        meta ibrname "${INCUS_BRIDGE}" meta obrname "${INCUS_BRIDGE}" log prefix "mmd-drop-bridge " level info limit rate 5/minute
        meta ibrname "${INCUS_BRIDGE}" meta obrname "${INCUS_BRIDGE}" counter drop
    }
}

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

        # Replies to connections the HOST opened. Without this the chain drops
        # the return traffic of anything the host initiates toward a workspace
        # - the host could not even ping its own containers, and the control
        # plane could not reach a published service. It does not weaken the
        # boundary: conntrack only matches flows the host started, so a
        # workspace still cannot open anything to the host itself.
        ct state established,related accept

        # The only host services a workspace legitimately needs are the
        # bridge's own DNS and DHCP (Incus's dnsmasq).
        udp dport 67 accept                                  # DHCP is broadcast
        ip daddr ${INCUS_BRIDGE_IP} udp dport 53 accept
        ip daddr ${INCUS_BRIDGE_IP} tcp dport 53 accept
        ip daddr ${INCUS_BRIDGE_IP} icmp type echo-request accept
${HOST_HTTPS_RULES}
        # Everything else from a workspace to this host is denied: sshd, the
        # dashboard on 443, Postgres, and the Incus API on 8443/9101.
        log prefix "mmd-drop-input " level info limit rate 5/minute
        drop
    }

    # Container -> elsewhere (routed).
    chain forward {
        type filter hook forward priority -10; policy accept;

        iifname != "${INCUS_BRIDGE}" accept

        # Replies belonging to already-permitted flows, including traffic
        # DNAT'd in from the internet to a published port.
        ct state established,related accept

        # Tenant-to-tenant, for anything that is actually ROUTED through this
        # hook. Note this rule does NOT cover two workspaces on the same bridge
        # talking to each other - see the bridge-family table above, which
        # is what does. Kept because it still catches routed paths.
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
After=incus.service
Requires=incus.service

# Deliberately NOT Wants=nftables.service. Ubuntu's /etc/nftables.conf begins
# with "flush ruleset", and Wants= starts a unit even when it is disabled - so
# pulling it in wiped EVERY table, including the one Incus owns. That removed
# the masquerade rule for the workspace bridge and silently killed all outbound
# connectivity from every workspace: DNS still resolved (dnsmasq is local) while
# anything leaving the host timed out. This unit loads only its own tables and
# never flushes.
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f ${RULES}
# Published customer ports live in their own file, rewritten by the
# provisioner. Loading it here means a reboot restores every reserved
# endpoint - customers are paying to keep those addresses stable.
ExecStart=-/usr/sbin/nft -f /etc/nftables/mmd-ports.nft
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable mmd-isolation.service >/dev/null 2>&1

log "active rules:"
nft list table inet mmd_isolation | sed 's/^/    /'
nft list table bridge mmd_bridge_isolation | sed 's/^/    /'
log "done - next: 40-swap-zram.sh"
