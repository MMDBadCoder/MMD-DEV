"""Passwords and sessions.

scrypt from the standard library rather than passlib/bcrypt: one less
dependency to keep patched on a host that is already a shared-kernel security
boundary, and no C extension to break across Python upgrades.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

_N, _R, _P, _DKLEN = 2 ** 15, 8, 1, 32
# scrypt needs 128 * N * r bytes = 32 MiB at these parameters, which is exactly
# OpenSSL's default maxmem ceiling - it raises "memory limit exceeded" without
# an explicit override. Set it above the requirement.
_MAXMEM = 128 * _N * _R * 2


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P,
                        dklen=_DKLEN, maxmem=_MAXMEM)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, dk_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p),
                            dklen=len(bytes.fromhex(dk_hex)),
                            maxmem=128 * int(n) * int(r) * 2)
    except (ValueError, TypeError):
        return False
    # Constant-time: a timing oracle on password comparison is a real leak.
    return hmac.compare_digest(dk.hex(), dk_hex)


def new_token() -> str:
    return secrets.token_urlsafe(32)
