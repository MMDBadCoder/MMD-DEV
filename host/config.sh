#!/usr/bin/env bash
# MMD-DEV shared host configuration.
# Sourced by every host/*.sh script. Override any value by exporting it first.

# Persistent local overrides, if the operator has any. Read BEFORE the
# defaults below, since every value here is assigned with `: "${VAR:=...}"` and
# so yields to anything already set.
#
# Without this, an override only lasts as long as the shell that exported it:
# re-running a script without it silently reverts to the default, which for a
# firewall rule means connectivity that worked yesterday is gone today with
# nothing to show why. Put durable settings in this file, not in your history.
if [ -r /etc/mmd/host.env ]; then
    set -a
    . /etc/mmd/host.env
    set +a
fi

# --- Incus / storage -------------------------------------------------------
: "${INCUS_POOL_NAME:=default}"          # Incus-side pool name
: "${ZPOOL_NAME:=mmdpool}"                # actual zpool name
: "${INCUS_POOL_FILE:=/var/lib/incus-pool.img}"
# Preallocated (NOT sparse) so ZFS can never believe it has space the host lacks.
: "${INCUS_POOL_SIZE_GIB:=68}"
: "${ZFS_ARC_MAX_BYTES:=536870912}"          # 512 MiB - essential on a 7.75 GiB host

# --- Network ---------------------------------------------------------------
: "${INCUS_BRIDGE:=incusbr0}"
: "${INCUS_BRIDGE_CIDR:=10.42.0.1/24}"
: "${INCUS_BRIDGE_SUBNET:=10.42.0.0/24}"
: "${INCUS_BRIDGE_IP:=10.42.0.1}"

# Workspace addresses permitted to reach this host's HTTPS port, space
# separated. Empty by default, and it should stay empty for every machine that
# does not need it.
#
# The isolation rules exist so a developer cannot reach out and touch the host,
# and this does not weaken that. The only port it opens is 443, which nginx
# already serves to the entire internet: a workspace on this list gains what
# every stranger already has, and gains nothing else. sshd, PostgreSQL, the
# Incus API on 8443/9101 and all of 127.0.0.0/8 stay refused for every
# workspace, listed or not.
#
# It exists for one case: the operator's own machine running the support agent,
# which must reach https://mmd-ai.ir/mcp to answer tickets. Traffic to the
# host's public address arrives on the input hook, because that address is
# local - so that is the chain which has to permit it, and permitting it there
# keeps the public hostname and its real certificate working unchanged instead
# of needing the name rewritten inside the machine.
: "${HOST_HTTPS_ALLOWED_WORKSPACES:=}"

# --- API endpoints (loopback only; nothing here is publicly reachable) ------
: "${INCUS_HTTPS_ADDRESS:=127.0.0.1:8443}"
: "${INCUS_METRICS_ADDRESS:=127.0.0.1:9101}"

# --- Swap ------------------------------------------------------------------
: "${ZRAM_SIZE_MB:=2048}"
: "${SWAPFILE_PATH:=/var/swapfile}"
: "${SWAPFILE_SIZE_GIB:=4}"

# --- Control plane service account ----------------------------------------
: "${MMD_USER:=mmd}"
: "${MMD_HOME:=/var/lib/mmd}"
: "${MMD_CERT_DIR:=/var/lib/mmd/certs}"

# --- Capacity accounting (seeds for the settings table) --------------------
# The control plane reads these from Postgres at runtime; these are the values
# the DB is seeded with on first migration.
: "${HOST_RESERVE_CORES:=1.0}"
: "${HOST_RESERVE_MEM_GIB:=2.0}"
: "${OVERCOMMIT_CPU:=2.0}"
: "${OVERCOMMIT_MEM:=1.0}"

# --- Default workspace tier ------------------------------------------------
: "${TIER_DEFAULT_CORES:=1}"
: "${TIER_DEFAULT_MEM_MIB:=1024}"
: "${TIER_DEFAULT_ROOT_GIB:=6}"
: "${TIER_DEFAULT_DOCKER_GIB:=8}"

# --- Golden image ----------------------------------------------------------
: "${BASE_IMAGE:=images:ubuntu/24.04/cloud}"
: "${GOLDEN_IMAGE_ALIAS:=mmd-workspace}"
: "${WORKSPACE_USER:=dev}"

# --- Uplink ----------------------------------------------------------------
# Derived, never written down. The host's public address and the provider LAN it
# sits on are facts about one particular server; hard-coding them into scripts
# put a live IP into the repository and made every check silently wrong on any
# other machine.
uplink_if()   { ip -o route get 1.1.1.1 2>/dev/null \
                  | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}'; }
uplink_addr() { ip -o -4 addr show "$(uplink_if)" 2>/dev/null \
                  | awk '{print $4}' | head -1 | cut -d/ -f1; }
# The provider's gateway - a neighbour on the LAN a tenant must not reach.
uplink_gw()   { ip -o -4 route show default 2>/dev/null \
                  | awk '{for(i=1;i<=NF;i++) if($i=="via") print $(i+1); exit}'; }
uplink_net()  { local c; c="$(ip -o -4 addr show "$(uplink_if)" 2>/dev/null \
                  | awk '{print $4}' | head -1)"
                python3 -c "import ipaddress,sys; print(ipaddress.ip_network(sys.argv[1], strict=False))" "$c"; }

# --- Helpers ---------------------------------------------------------------
log()  { printf '\033[1;34m[mmd]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mmd]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[mmd]\033[0m %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "must run as root"; }
