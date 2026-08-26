"""Usernames, which are also DNS labels.

A customer's Hermes dashboard is served at `hermes.<username>.mmd-ai.ir`, so a
username is not a display name - it is part of a hostname, and anything that is
not a valid DNS label breaks the address rather than merely looking odd. That is
why the rules here are stricter than a username field usually is: no dots (they
would create another level of subdomain), no underscores (not legal in a
hostname), lowercase only (DNS is case-insensitive, so `Ali` and `ali` are the
same host and must not be two accounts).

Kept free of database and framework imports so the rules can be tested on their
own, and so there is exactly one definition of what a username may be.
"""
from __future__ import annotations

import re

MIN_LEN = 3
MAX_LEN = 32          # a DNS label allows 63; 32 keeps the address readable

_VALID = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

# Names that would collide with the platform's own hostnames, or that a customer
# reading the address would reasonably mistake for an official one. Blocked
# whether or not those hosts exist yet, because taking one back later means
# renaming a live customer's dashboard.
RESERVED = {
    "www", "api", "admin", "root", "hermes", "claude", "mail", "smtp", "imap",
    "ftp", "ns", "ns1", "ns2", "dns", "mx", "cdn", "static", "assets", "app",
    "dashboard", "console", "panel", "support", "help", "docs", "status",
    "billing", "pay", "payment", "account", "accounts", "login", "signin",
    "signup", "register", "auth", "oauth", "sso", "test", "demo", "example",
    "localhost", "mmd", "mmddev", "openrouter", "anthropic", "system",
}


class UsernameError(ValueError):
    """Carries a machine-readable code so the interface can translate it."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalise(raw: str) -> str:
    """Lowercase and trim. Applied before validating AND before comparing, so
    `Ali` and `ali` cannot become two accounts pointing at one hostname."""
    return (raw or "").strip().lower()


def validate(raw: str) -> str:
    """Return the normalised username, or raise UsernameError."""
    name = normalise(raw)
    if not name:
        raise UsernameError("username_required", "Choose a username.")
    if len(name) < MIN_LEN:
        raise UsernameError("username_short",
                            f"A username must be at least {MIN_LEN} characters.")
    if len(name) > MAX_LEN:
        raise UsernameError("username_long",
                            f"A username may be at most {MAX_LEN} characters.")
    if not _VALID.match(name):
        raise UsernameError(
            "username_charset",
            "A username may use only lowercase letters, digits and hyphens, "
            "and may not begin or end with a hyphen.")
    if name in RESERVED:
        raise UsernameError("username_reserved", "That username is reserved.")
    # An all-numeric label is legal DNS but reads as an address fragment and
    # invites confusion with a workspace index.
    if name.isdigit():
        raise UsernameError("username_numeric",
                            "A username must contain at least one letter.")
    return name


def derive_from_email(email: str) -> str:
    """A starting username for an account that predates the field.

    Used to backfill existing customers, who never got to choose. The local part
    is sanitised rather than rejected - `seyfoori.amir.h` becomes
    `seyfoori-amir-h`, because the alternative is an account with no address.
    """
    local = normalise(email).split("@", 1)[0]
    name = re.sub(r"[^a-z0-9-]+", "-", local).strip("-")
    name = re.sub(r"-{2,}", "-", name)[:MAX_LEN].strip("-")
    if len(name) < MIN_LEN:
        name = (name + "-user")[:MAX_LEN].strip("-")
    if name in RESERVED or name.isdigit():
        name = f"{name}-1"[:MAX_LEN].strip("-")
    return name


def make_unique(candidate: str, taken: set[str]) -> str:
    """Append the smallest numeric suffix that clears a collision."""
    if candidate not in taken:
        return candidate
    stem = candidate[: MAX_LEN - 3].rstrip("-")
    n = 2
    while f"{stem}-{n}" in taken:
        n += 1
    return f"{stem}-{n}"


def hermes_host(username: str, domain: str) -> str:
    """Where this customer's Hermes dashboard lives."""
    return f"hermes.{normalise(username)}.{domain}"
