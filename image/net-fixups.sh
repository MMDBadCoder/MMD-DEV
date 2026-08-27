#!/usr/bin/env bash
# Restore the file capabilities dpkg could not set. Idempotent; safe to run on
# every provision and on a long-lived workspace afterwards.
#
# WHY THIS EXISTS
# ---------------
# Reported as: `ping google.com` inside a workspace answers
#
#     ping: socktype: SOCK_RAW
#     ping: socket: Operation not permitted
#     ping: => missing cap_net_raw+p capability or setuid?
#
# `ping` needs one of two things and, in a fresh workspace, has neither.
#
#   1. An unprivileged ICMP datagram socket, allowed only for the group range in
#      net.ipv4.ping_group_range. A NEW network namespace does not inherit the
#      host's value: measured, the host has "0 2147483647" while a workspace has
#      "65534 65534", and the dev user's gid is 1002 - outside it. That sysctl
#      also CANNOT be set from inside: /proc/sys/net is not writable from the
#      container's user namespace, so `sysctl -w` there is refused.
#   2. cap_net_raw on the binary. Ubuntu's iputils-ping postinst sets this with
#      setcap - and that call fails silently when dpkg runs inside an
#      unprivileged container, which is why the file arrives with no capability
#      at all.
#
# So the capability is set here, from inside the workspace, where it lands in
# that workspace's own user namespace.
#
# WHY NOT IN THE GOLDEN IMAGE
# ---------------------------
# Because it would not survive. The workspaces run with
# security.idmap.isolated=true, so every one of them has its OWN uid range. A
# v3 security.capability xattr embeds the rootid of the namespace it was set
# in, so a capability baked into the shared image carries the BUILD container's
# rootid and simply does not apply in a workspace mapped somewhere else. It has
# to be set per workspace, which is what this script is for.
#
# It also has to be re-appliable: `apt upgrade` replaces /bin/ping and takes the
# capability with it.
set -eu

# Prefix for every path below. Empty in production; the test suite points it at
# a temporary tree so the script's BEHAVIOUR can be exercised - is it
# idempotent, does it skip a binary that is not installed, does a refused
# setcap abort the run - rather than its text grepped for keywords.
ROOT="${MMD_FIXUP_ROOT:-}"

# Binaries that ship expecting a capability the package manager could not grant.
# `ping` is the one customers notice; the others are the same failure and the
# same fix, applied only when the file is actually present.
set_cap() {
  cap="$1"; path="${ROOT}$2"
  [ -e "$path" ] || return 0
  current="$(getcap "$path" 2>/dev/null || true)"
  case "$current" in
    *"$cap"*) echo "  $path already has $cap" ; return 0 ;;
  esac
  if setcap "${cap}+p" "$path" 2>/dev/null; then
    echo "  $path granted $cap"
  else
    # Not fatal. A workspace whose kernel or filesystem refuses file
    # capabilities still works for everything except this one binary, and
    # failing the whole provision over ping would be the wrong trade.
    echo "  $path could NOT be granted $cap (continuing)"
  fi
}

command -v setcap >/dev/null 2>&1 || {
  echo "  libcap2-bin missing; installing"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libcap2-bin >/dev/null 2>&1 || true
}

set_cap cap_net_raw /bin/ping
set_cap cap_net_raw /usr/bin/ping
set_cap cap_net_raw /bin/ping6
set_cap cap_net_raw /usr/bin/ping6
# traceroute and mtr fail the same way and for the same reason.
set_cap cap_net_raw /usr/bin/mtr-packet
set_cap cap_net_raw /usr/bin/traceroute6
echo "net fixups done"
