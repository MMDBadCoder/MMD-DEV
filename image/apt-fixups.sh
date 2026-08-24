#!/usr/bin/env bash
# Make ordinary `apt install` work inside a workspace. Idempotent; safe to run
# on a fresh image at build time and on a long-lived workspace afterwards.
#
# WHY THIS EXISTS
# ---------------
# Ubuntu's `firefox` package is a 77 kB transitional stub whose only job is to
# install the firefox SNAP. snapd cannot work inside an unprivileged container:
# the snap install hook dies with
#
#     cannot fstatat canonical snap directory: Permission denied
#
# because snap-confine needs mount and AppArmor privileges the container does
# not have - and must never be given, since they are the isolation this whole
# product rests on. Worse, the failure leaves dpkg mid-transaction, so the
# customer's NEXT apt command fails too and the machine simply looks broken.
#
# The fix is Mozilla's own APT repository, which ships Firefox as a real .deb
# with no snap involved, plus the APT pin Mozilla documents. The pin is not
# optional: Ubuntu's stub carries an epoch ("1:1snap1-0ubuntu5") which sorts
# ABOVE any plain Mozilla version, so without a priority of 1000 the stub keeps
# winning and `apt install firefox` keeps failing.
#
# NB: no `pipefail`, and no `cmd | grep` tests. `grep -q` exits at the first
# match and SIGPIPEs its producer, which pipefail then reports as a failure -
# a trap this repository has fallen into twice. Every check below is a plain
# command substitution matched with `case`.
set -eu
export DEBIAN_FRONTEND=noninteractive

KEYRING=/etc/apt/keyrings/packages.mozilla.org.asc
LIST=/etc/apt/sources.list.d/mozilla.list
PIN=/etc/apt/preferences.d/mozilla

# --- 1. clear the wreckage of an earlier `apt install firefox` --------------
# dpkg leaves the stub half-unpacked ("iF"/"iU"), and refuses every subsequent
# operation until that is resolved. A customer who hit the bug before this
# script ran needs the machine put back into a usable state, not just fixed
# going forward.
state="$(dpkg-query -W -f='${db:Status-Abbrev}' firefox 2>/dev/null || true)"
case "$state" in
  ""|ii*) : ;;
  *) dpkg --remove --force-remove-reinstreq firefox >/dev/null 2>&1 || true ;;
esac
dpkg --configure -a >/dev/null 2>&1 || true

# --- 2. Mozilla's signing key ----------------------------------------------
install -d -m 0755 /etc/apt/keyrings
if [ ! -s "$KEYRING" ]; then
  curl -fsSL --retry 3 https://packages.mozilla.org/apt/repo-signing-key.gpg \
    -o "$KEYRING"
  chmod a+r "$KEYRING"
fi

# --- 3. the repository ------------------------------------------------------
cat > "$LIST" <<LIST_EOF
# Firefox and Thunderbird as real .deb packages. Ubuntu ships only snap stubs,
# and snaps cannot run in this machine. Managed by MMD-DEV; edits may be
# overwritten.
deb [signed-by=$KEYRING] https://packages.mozilla.org/apt mozilla main
LIST_EOF

# --- 4. the pin -------------------------------------------------------------
cat > "$PIN" <<'PIN_EOF'
Package: *
Pin: origin packages.mozilla.org
Pin-Priority: 1000
PIN_EOF

# --- 5. remove snapd --------------------------------------------------------
# It cannot function here, it runs two daemons that cost memory a 1 GiB machine
# cannot spare, and leaving it installed only makes snap-backed packages look
# available right up until they fail. Removing it turns a confusing mid-install
# failure into an honest "package not available".
snapd_state="$(dpkg-query -W -f='${Status}' snapd 2>/dev/null || true)"
case "$snapd_state" in
  *"ok installed"*)
    systemctl disable --now snapd.service snapd.socket snapd.seeded.service \
      >/dev/null 2>&1 || true
    apt-get purge -y -qq snapd >/dev/null 2>&1 || true
    apt-get autoremove -y -qq >/dev/null 2>&1 || true
    rm -rf /snap /var/snap /var/lib/snapd /root/snap
    ;;
esac

apt-get update -qq
echo "mmd-apt-fixups: ok"
