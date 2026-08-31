"""Periodic Postgres backups, delivered to an administrator's Telegram.

Off-host is the whole point. A dump written to this machine's disk survives a
dropped table and nothing else - not the disk, not the provider, not the
machine going away - and this platform runs on ONE host. Telegram is chosen
because it needs no second account, no bucket, no credential rotation and no
egress rules: the operator already has the app, and a bot token plus a chat id
is the entire configuration.

Three things follow from that choice and shape everything below:

  * The bot is the ADMIN's, not a customer's, and not the one Hermes and
    OpenClaw share. Those are wired to agents with a shell in someone's
    workspace; the platform's database is not going through the same bot.
  * Telegram caps bot uploads at 50 MB. That is a hard ceiling, so the size is
    checked before the upload rather than discovered as a failed request, and
    the reason is recorded where an admin will see it.
  * A dump is EVERYTHING - password hashes, session material, provider keys,
    every customer's ledger. Sending it somewhere is a deliberate act, so it is
    off by default, admin-only, and audited when switched on.

The format is pg_dump's custom format (`-Fc`): compressed already, so there is
no second gzip step, and restorable selectively with pg_restore, which a plain
SQL file is not.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from .config import CONFIG
from .models import Setting

log = logging.getLogger(__name__)

SETTING_ENABLED = "backup_enabled"
SETTING_INTERVAL = "backup_interval_minutes"
SETTING_TOKEN = "backup_bot_token"
SETTING_CHAT = "backup_chat_id"
SETTING_LAST_OK = "backup_last_ok_at"
SETTING_LAST_ERROR = "backup_last_error"
SETTING_LAST_SIZE = "backup_last_size"

# Five minutes is not a taste. Every run is a full dump and a full upload, and
# an interval shorter than the work takes would queue backups behind each other
# forever. The ceiling is a week, past which "periodic" is not what this is.
MIN_INTERVAL = 5
MAX_INTERVAL = 10080
DEFAULT_INTERVAL = 1440

# Telegram's documented ceiling for a bot upload. Checked locally so an
# oversized dump is reported as a size problem rather than an HTTP failure.
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024

DUMP_TIMEOUT = 900
SEND_TIMEOUT = 600

TOKEN_RE = re.compile(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,}")
CHAT_RE = re.compile(r"-?[1-9][0-9]{4,15}")


class BackupError(Exception):
    """Anything that stops a backup reaching Telegram."""


# --- settings -------------------------------------------------------------
def _get(db, key: str, default: str = "") -> str:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    return row.value if row and row.value is not None else default


def _set(db, key: str, value: str) -> None:
    row = db.execute(select(Setting).where(Setting.key == key)).scalar_one_or_none()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    # Sessions here are autoflush=False, so a pending INSERT is invisible to
    # the next SELECT in the same call. Everything below reads back what it
    # just wrote - `save` re-reads the token, and returns `config` - so without
    # this a first-time token would appear unset the instant after storing it.
    db.flush()


def clamp_interval(minutes) -> int:
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, value))


def config(db) -> dict:
    """Everything the admin page needs, and no secret.

    The token is reported as "set or not" plus its last four characters. An
    operator needs to recognise which bot is configured; nobody needs the panel
    to hand the token back, and a page that never receives it cannot leak it.
    """
    token = _get(db, SETTING_TOKEN)
    return {
        "enabled": _get(db, SETTING_ENABLED) == "1",
        "interval_minutes": clamp_interval(_get(db, SETTING_INTERVAL, DEFAULT_INTERVAL)),
        "chat_id": _get(db, SETTING_CHAT),
        "bot_token_set": bool(token),
        "bot_token_hint": token[-4:] if token else "",
        "last_ok_at": _get(db, SETTING_LAST_OK) or None,
        "last_error": _get(db, SETTING_LAST_ERROR) or None,
        "last_size": int(_get(db, SETTING_LAST_SIZE, "0") or 0),
        "min_interval": MIN_INTERVAL,
        "max_interval": MAX_INTERVAL,
        "max_bytes": TELEGRAM_MAX_BYTES,
    }


def save(db, *, enabled: bool, interval_minutes, chat_id: str,
         bot_token: str | None) -> dict:
    """Apply an admin's settings.

    `bot_token=None` (an empty field) means KEEP the stored one. A page that
    cannot display the token would otherwise erase it on every unrelated save -
    an admin changing the interval would silently turn delivery off.
    """
    chat_id = (chat_id or "").strip()
    if bot_token is not None:
        bot_token = bot_token.strip()
        if bot_token and not TOKEN_RE.fullmatch(bot_token):
            raise BackupError("invalid token")
    if chat_id and not CHAT_RE.fullmatch(chat_id):
        raise BackupError("invalid chat id")

    # Everything is validated before anything is written, so a rejected save
    # leaves the previous settings whole rather than half-applied.
    effective_token = bot_token if bot_token is not None else _get(db, SETTING_TOKEN)

    # Refusing to arm a schedule that cannot deliver. Enabled-but-unconfigured
    # would sit there logging the same failure every interval while the page
    # said backups were on.
    if enabled and not (effective_token and chat_id):
        raise BackupError("token and chat id are required")

    if bot_token is not None:
        _set(db, SETTING_TOKEN, bot_token)
    _set(db, SETTING_CHAT, chat_id)
    _set(db, SETTING_INTERVAL, str(clamp_interval(interval_minutes)))
    _set(db, SETTING_ENABLED, "1" if enabled else "0")
    # A settings change clears the last failure: it is almost always what the
    # admin just fixed, and leaving it up makes a repaired backup look broken
    # until the next run.
    _set(db, SETTING_LAST_ERROR, "")
    return config(db)


def due(db, now: datetime | None = None) -> bool:
    """Whether a run is owed.

    Measured from the last SUCCESS, not the last attempt, so a run that fails
    is retried on the normal cadence instead of being skipped until the failure
    happens to age out.
    """
    if _get(db, SETTING_ENABLED) != "1":
        return False
    now = now or datetime.now(timezone.utc)
    last = _get(db, SETTING_LAST_OK)
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    interval = clamp_interval(_get(db, SETTING_INTERVAL, DEFAULT_INTERVAL))
    return now - when >= timedelta(minutes=interval)


# --- the backup itself ----------------------------------------------------
def dump_url(url: str | None = None) -> str:
    """The database URL in the form libpq accepts.

    SQLAlchemy's URLs carry the driver in the scheme - `postgresql+psycopg://`.
    pg_dump takes a plain `postgresql://`, and rejects the driver form.
    """
    url = url or CONFIG.database_url
    return re.sub(r"^postgresql\+[a-z0-9_]+://", "postgresql://", url)


def dump_to(path: str, url: str | None = None) -> int:
    """Write a custom-format dump and return its size."""
    proc = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--no-privileges",
         "--file", path, dump_url(url)],
        capture_output=True, text=True, timeout=DUMP_TIMEOUT, check=False)
    if proc.returncode != 0:
        # stderr, not the command line: the URL carries the password.
        raise BackupError(f"pg_dump failed: {(proc.stderr or '').strip()[-200:]}")
    size = os.path.getsize(path)
    if size <= 0:
        raise BackupError("pg_dump produced an empty file")
    return size


def send_document(token: str, chat_id: str, path: str, caption: str) -> None:
    """Upload one file to one chat."""
    with open(path, "rb") as fh:
        try:
            r = httpx.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"document": (os.path.basename(path), fh,
                                    "application/octet-stream")},
                timeout=SEND_TIMEOUT)
        except httpx.HTTPError as e:
            raise BackupError(f"telegram unreachable: {e}") from e
    if r.status_code >= 400:
        # Telegram's own description is the diagnosis - a wrong chat id and a
        # revoked token are both 400 and otherwise indistinguishable.
        raise BackupError(f"telegram rejected the upload: {r.text[:200]}")


def run_once(db, now: datetime | None = None) -> dict:
    """Dump, send, and record the outcome. Never raises.

    The result is written to settings rather than only logged, because the
    admin page is where someone looks to find out whether backups are actually
    arriving, and "enabled" on its own does not answer that.
    """
    now = now or datetime.now(timezone.utc)
    token, chat = _get(db, SETTING_TOKEN), _get(db, SETTING_CHAT)
    if not (token and chat):
        return {"ok": False, "error": "not configured"}

    stamp = now.strftime("%Y%m%d-%H%M%S")
    name = f"mmd-{stamp}.dump"
    try:
        # PrivateTmp puts this inside the worker unit's own namespace, so the
        # dump is not visible to the internet-facing process even for the few
        # seconds it exists, and is gone when the unit restarts.
        with tempfile.TemporaryDirectory(prefix="mmd-backup-") as tmp:
            path = os.path.join(tmp, name)
            size = dump_to(path)
            if size > TELEGRAM_MAX_BYTES:
                raise BackupError(
                    f"dump is {size} bytes, over Telegram's "
                    f"{TELEGRAM_MAX_BYTES} byte limit for bots")
            send_document(token, chat, path,
                          f"MMD-DEV — {stamp} — {size} bytes")
    except BackupError as e:
        _set(db, SETTING_LAST_ERROR, str(e)[:255])
        db.commit()
        log.warning("backup failed: %s", e)
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        _set(db, SETTING_LAST_ERROR, f"{type(e).__name__}: {e}"[:255])
        db.commit()
        log.exception("backup failed")
        return {"ok": False, "error": str(e)}

    _set(db, SETTING_LAST_OK, now.isoformat())
    _set(db, SETTING_LAST_SIZE, str(size))
    _set(db, SETTING_LAST_ERROR, "")
    db.commit()
    log.info("backup sent: %s (%s bytes)", name, size)
    return {"ok": True, "size": size, "name": name}
