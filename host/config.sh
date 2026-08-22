#!/usr/bin/env bash
# MMD-DEV shared host configuration.
# Sourced by every host/*.sh script. Override any value by exporting it first.

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
: "${TIER_DEFAULT_DOCKER_GIB:=4}"

# --- Golden image ----------------------------------------------------------
: "${BASE_IMAGE:=images:ubuntu/24.04/cloud}"
: "${GOLDEN_IMAGE_ALIAS:=mmd-workspace}"
: "${WORKSPACE_USER:=dev}"

# --- Helpers ---------------------------------------------------------------
log()  { printf '\033[1;34m[mmd]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[mmd]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[mmd]\033[0m %s\n' "$*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "must run as root"; }
