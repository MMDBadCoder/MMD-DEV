"""No English prose may reach a Persian interface.

A customer opened a ticket reading, in full: *"this text was English, it must be
Persian"*, quoting a message the dashboard had printed at them. The specific
string was easy to fix. The class of bug was not: the API is written in English
and the interface is Persian, so any sentence that slips into a response body
lands in front of a customer untranslated.

The contract is that the API returns stable CODES and the interface translates
them - `fail()` messages are fine, because api.js resolves those by code and
never shows the English. Anything else returned as prose is a leak.

This walks the API's return statements and fails on English sentences, so the
next one is caught before a customer has to report it.
"""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "control" / "mmd" / "app.py"
SOURCE = APP.read_text()
TREE = ast.parse(SOURCE)

# Two or more words starting with a capital: a sentence, not an identifier, a
# code, a MIME type or a format string. Two rather than four because
# "Password changed." was the one that slipped past a stricter threshold.
PROSE = re.compile(r"^[A-Z][a-z]+(?:\s+[A-Za-z][\w'-]*){1,}")

# Keys whose values are structural rather than displayed. `label` is a size name
# like "1 vCPU / 2 GB" that the interface formats itself; `detail` is audit data.
NON_DISPLAY_KEYS = {"detail", "label", "command", "address", "output"}


def _strings_in(node):
    """Every literal string a dict/list value could carry, including f-string
    pieces - the reported bug was an f-string."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            yield sub.value


def _returned_dicts():
    for fn in ast.walk(TREE):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Only the request handlers; helpers return internal shapes.
        decorated = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and isinstance(d.func.value, ast.Name) and d.func.value.id == "app"
                        for d in fn.decorator_list)
        if not decorated:
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                yield fn.name, node.value


def test_no_endpoint_returns_an_english_sentence():
    offenders = []
    for fname, d in _returned_dicts():
        for key, value in zip(d.keys, d.values):
            kname = key.value if isinstance(key, ast.Constant) else "?"
            if kname in NON_DISPLAY_KEYS:
                continue
            for text in _strings_in(value):
                if PROSE.match(text.strip()):
                    offenders.append(f"{fname}: {kname!r} = {text.strip()[:70]!r}")
    assert offenders == [], (
        "English prose returned to a Persian interface; return a code instead "
        "and add the wording to web/js/i18n.js:\n  " + "\n  ".join(offenders))


def test_the_websocket_never_writes_english_into_the_terminal():
    """Terminal output is the one channel the interface cannot translate after
    the fact - it arrives as bytes on the customer's screen, not as data."""
    offenders = []
    for node in ast.walk(TREE):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in ("send_text", "send_bytes"):
            continue
        for text in _strings_in(node):
            if PROSE.match(re.sub(r"\\[rnx][0-9a-fA-F]*|\x1b\[[\d;]*m", " ", text).strip()):
                offenders.append(text.strip()[:70])
    assert offenders == [], f"English written into a terminal stream: {offenders}"


def test_the_reported_message_is_gone():
    """The exact string from the ticket."""
    assert "credits to run for" not in SOURCE
    assert "Your balance is" not in SOURCE


def test_the_blocked_reason_carries_numbers_not_a_sentence():
    """The interface has to format the amount as Toman with Persian digits,
    which it can only do if it is given the number."""
    assert '"code": "insufficient_credit"' in SOURCE
    assert '"need": need / MICRO' in SOURCE
    assert '"have": have / MICRO' in SOURCE


def test_the_signup_page_no_longer_matches_on_english():
    """It compared the server's exact English sentence to choose which Persian
    message to show, so rewording the server would have broken it silently."""
    auth = (ROOT / "web" / "js" / "pages" / "auth.js").read_text()
    assert "Your account is awaiting approval" not in auth
    assert 'r.code === "admin_created"' in auth


def test_every_code_the_api_can_send_has_a_translation():
    """The codes introduced with this change, checked against the catalogue."""
    i18n = (ROOT / "web" / "js" / "i18n.js").read_text()
    for key in ("blocked.insufficient_credit", "blocked.capacity_memory",
                "blocked.capacity_cpu", "blocked.capacity_general",
                "auth.made.pending", "auth.made.admin",
                "ports.warn.discouraged_port",
                "term.closed.machineoff", "term.closed.nomachine",
                "term.closed.denied"):
        assert f'"{key}"' in i18n, f"missing translation for {key}"
