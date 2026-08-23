#!/usr/bin/env bash
# Verify the P0 host foundation. Read-only.
# NB: no `pipefail` here. Every check is a `producer | grep -q` pipeline, and
# grep -q exits on first match - killing the producer with SIGPIPE, which
# pipefail would then report as a failed check. Bit us twice already.
set -u
cd "$(dirname "$0")/../host" && . ./config.sh
pass=0; fail=0
chk() { if eval "$2" >/dev/null 2>&1; then printf '  \033[1;32mOK\033[0m    %s\n' "$1"; pass=$((pass+1));
        else printf '  \033[1;31mFAIL\033[0m  %s\n' "$1"; fail=$((fail+1)); fi; }

log "P0 foundation verification"
chk "incus daemon active"              'systemctl is-active --quiet incus'
chk "incus is container-only build"    '! dpkg -s qemu-system-x86 2>/dev/null | grep -q "^Status: install"'
chk "LXD snap absent"                  '! snap list lxd'
chk "host Docker absent"               '! command -v docker'
chk "zpool $ZPOOL_NAME online"         "zpool list -H -o health $ZPOOL_NAME | grep -q ONLINE"
chk "pool file is NOT sparse"          '[ "$(du -m --apparent-size '"$INCUS_POOL_FILE"' | cut -f1)" -le "$(du -m '"$INCUS_POOL_FILE"' | cut -f1)" ]'
chk "lz4 compression on"               "zfs get -H -o value compression $ZPOOL_NAME | grep -q lz4"
chk "pool defaults: refquota"          'incus storage get '"$INCUS_POOL_NAME"' volume.zfs.use_refquota | grep -q true'
chk "pool defaults: reserve_space"     'incus storage get '"$INCUS_POOL_NAME"' volume.zfs.reserve_space | grep -q true'
chk "ZFS ARC capped"                   '[ "$(cat /sys/module/zfs/parameters/zfs_arc_max)" = "'"$ZFS_ARC_MAX_BYTES"'" ]'
chk "bridge $INCUS_BRIDGE exists"      "ip link show $INCUS_BRIDGE"
# Without this, the SECOND workspace to start fails with "Instance DNS name
# already used on network" - every instance is named "ws" in its own project
# and DNS registration is per-network. A one-tenant test never sees it.
chk "bridge DNS registration disabled" '[ "$(incus network get '"$INCUS_BRIDGE"' dns.mode)" = none ]' 
chk "incus API loopback-only"          'ss -tlnH sport = :8443 | grep -q "127.0.0.1:8443"'
chk "metrics loopback-only"            'ss -tlnH sport = :9101 | grep -q "127.0.0.1:9101"'
chk "isolation table loaded"           'nft list table inet mmd_isolation'
chk "metadata IP blocked"              'nft list table inet mmd_isolation | grep -q "169.254.0.0/16"'
chk "uplink LAN blocked"               'nft list table inet mmd_isolation | grep -q "203.0.113.0/24"'
chk "isolation applies at boot"        'systemctl is-enabled --quiet mmd-isolation.service'
# Incus owns its own nftables table, including the masquerade that gives every
# workspace outbound internet. Anything running `flush ruleset` destroys it -
# which is exactly what Ubuntu's /etc/nftables.conf does on the first line. The
# symptom is nasty to diagnose: DNS keeps working (dnsmasq is local) while
# everything leaving the host times out.
chk "incus firewall table present"     'nft list table inet incus'
chk "workspace masquerade present"     'nft list table inet incus | grep -q masquerade'
chk "nftables.service cannot flush us" '[ "$(systemctl is-enabled nftables 2>&1)" = masked ]'
chk "zram swap active"                 'swapon --show --noheadings | grep -q zram'
chk "disk swap active"                 "swapon --show --noheadings | grep -q $SWAPFILE_PATH"
# NB: these assert the operator is NOT locked out. An earlier version asserted
# the opposite - that password auth was disabled - so a lockout registered as a
# passing check while the operator could not reach their own machine.
chk "sshd is running"                  'systemctl is-active --quiet ssh'
chk "sshd config is valid"             'sshd -t'
chk "a login method is available"      'sshd -T | grep -qiE "^(passwordauthentication|pubkeyauthentication) yes"'
chk "empty passwords refused"          'sshd -T | grep -qi "^permitemptypasswords no"'
chk "journald capped"                  'grep -q SystemMaxUse /etc/systemd/journald.conf.d/60-mmd.conf'
chk "unattended-upgrades enabled"      'systemctl is-enabled --quiet unattended-upgrades'
echo
log "P0: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
