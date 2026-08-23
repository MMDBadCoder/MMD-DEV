"""Validation of customer-supplied SSH public keys.

These lines are written into a root-owned process's view of
`~/.ssh/authorized_keys`, and that file format is more dangerous than it looks:
a line may begin with an OPTIONS field, and one of the options is
`command="..."`, which runs on every connection. Others force port forwarding
or environment variables.

So keys are not "sanitised" - they are parsed, and anything that is not exactly
`<type> <base64> [comment]` is refused. An options prefix is never accepted,
even a harmless-looking one.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re

# The types OpenSSH still accepts. ssh-dss (DSA) is deliberately absent: it is
# disabled by default in modern OpenSSH and is too weak to offer.
ALLOWED_TYPES = {
    "ssh-ed25519",
    "sk-ssh-ed25519@openssh.com",
    "ssh-rsa",
    "rsa-sha2-256",
    "rsa-sha2-512",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}

MAX_KEYS = 10
MAX_LINE = 8192
_B64 = re.compile(r"^[A-Za-z0-9+/]+={0,3}$")
_COMMENT_OK = re.compile(r"^[\w.@+/:\- ]{0,200}$")


class KeyError_(ValueError):
    """Invalid public key. Named with a trailing underscore to avoid shadowing
    the builtin."""

    def __init__(self, message: str, code: str = "bad_ssh_key"):
        super().__init__(message)
        self.code = code


def parse_key(line: str) -> tuple[str, str, str]:
    """Parse one public key line. Returns (type, base64, comment).

    Raises KeyError_ on anything that is not a plain key line.
    """
    line = line.strip()
    if not line or line.startswith("#"):
        raise KeyError_("Empty line", "bad_ssh_key")
    if len(line) > MAX_LINE:
        raise KeyError_("Key line is too long", "bad_ssh_key")
    if "\n" in line or "\r" in line:
        raise KeyError_("Key must be a single line", "bad_ssh_key")

    parts = line.split(None, 2)
    if len(parts) < 2:
        raise KeyError_("A key needs a type and a body", "bad_ssh_key")

    ktype, body = parts[0], parts[1]
    comment = parts[2] if len(parts) > 2 else ""

    if ktype not in ALLOWED_TYPES:
        # This is also what rejects an options prefix: a line starting with
        # `command="..."` or `no-pty,` has that text as its first field, which
        # is not a key type.
        raise KeyError_(
            f"{ktype[:40]!r} is not a supported key type. "
            "Options such as command= are not accepted.", "bad_ssh_key_type")

    if not _B64.match(body):
        raise KeyError_("The key body is not valid base64", "bad_ssh_key")
    try:
        raw = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        raise KeyError_("The key body is not valid base64", "bad_ssh_key")
    if len(raw) < 16:
        raise KeyError_("The key body is too short to be real", "bad_ssh_key")

    # The blob starts with a length-prefixed copy of the type; if it disagrees
    # with the text field the line is malformed or hand-edited.
    n = int.from_bytes(raw[:4], "big")
    if n > len(raw) - 4 or raw[4:4 + n].decode("ascii", "replace") != ktype:
        raise KeyError_("The key body does not match its stated type", "bad_ssh_key")

    if comment and not _COMMENT_OK.match(comment):
        raise KeyError_("The key comment contains unsupported characters", "bad_ssh_key")

    return ktype, body, comment


def fingerprint(body: str) -> str:
    """OpenSSH-style SHA256 fingerprint, for showing the customer which key
    they installed."""
    digest = hashlib.sha256(base64.b64decode(body)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def normalise(text: str) -> list[dict]:
    """Validate a block of pasted keys. Returns one entry per key."""
    lines = [l for l in (text or "").splitlines() if l.strip() and not l.strip().startswith("#")]
    if not lines:
        raise KeyError_("No key was provided", "no_ssh_key")
    if len(lines) > MAX_KEYS:
        raise KeyError_(f"At most {MAX_KEYS} keys", "too_many_ssh_keys")

    out, seen = [], set()
    for line in lines:
        ktype, body, comment = parse_key(line)
        if body in seen:
            continue
        seen.add(body)
        out.append({"type": ktype, "body": body, "comment": comment,
                    "fingerprint": fingerprint(body),
                    "line": f"{ktype} {body}" + (f" {comment}" if comment else "")})
    return out


def authorized_keys_body(keys: list[dict]) -> str:
    header = "# Managed by MMD-DEV. Edits here are replaced when keys change.\n"
    return header + "\n".join(k["line"] for k in keys) + "\n"
