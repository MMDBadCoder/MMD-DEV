"""The desktop password is required on every switch-on.

It used to be required only on the first enable. A later enable then reused
whatever was set during an earlier session - handing the customer a desktop on a
public port guarded by a credential they may not remember, and which a previous
holder of the machine might still know.

app.py cannot be imported without a database and a config file, so the rule is
checked against its source. Crude, but it fails loudly if someone reinstates the
`and not ws.rdp_installed` condition, which is the actual regression to catch.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "control" / "mmd" / "app.py").read_text()
CONN = (ROOT / "web" / "js" / "pages" / "connections.js").read_text()

# The body of set_rdp, from its decorator to the next one.
RDP = APP[APP.index('@app.post("/api/workspace/services/rdp")'):]
RDP = RDP[:RDP.index("@app.post", 10)]


def test_the_password_check_is_unconditional():
    assert 'if not body.password:' in RDP
    # The old rule, which must not come back.
    assert "not ws.rdp_installed)" not in RDP.replace(" ", "").replace("\n", "")
    assert "and not ws.rdp_installed" not in RDP


def test_the_password_is_always_sent_to_the_provisioner():
    """Previously the key was added only when a password was supplied, so an
    enable with no password silently kept the old one."""
    assert '"password": body.password' in RDP


def test_a_short_password_gets_a_translatable_code_not_a_422():
    """Pydantic's own min_length produces a 422 whose body the interface cannot
    translate, so the customer sees an English validation dump."""
    assert 'rdp_password_short' in RDP
    model = APP[APP.index("class RdpRequest"):]
    model = model[:model.index("class ", 10)]
    # A comment explains why min_length is absent, so judge the code only.
    code = "\n".join(ln for ln in model.splitlines() if not ln.lstrip().startswith("#"))
    assert "min_length" not in code


def test_the_error_codes_are_translated():
    i18n = (ROOT / "web" / "js" / "i18n.js").read_text()
    assert '"err.rdp_needs_password"' in i18n
    assert '"err.rdp_password_short"' in i18n


def test_the_form_always_offers_the_field():
    """It used to render a "change password" label and treat the field as
    optional once installed."""
    assert "rdp.changepw" not in CONN
    assert 'id="rdppw"' in CONN
    assert 'required' in CONN


def test_the_form_blocks_before_the_round_trip():
    assert re.search(r"enabling && pw\.length < 8", CONN)
