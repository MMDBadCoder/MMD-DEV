"""The terminal must not clip its own bottom line.

Reported by a customer: "the bottom of the terminal is not visible and I cannot
see the last line". Measured cause: xterm's FitAddon sizes the terminal from
`getComputedStyle(parent).height` and subtracts only the padding of xterm's OWN
element - never the parent's. Under the stylesheet's global `border-box`, Chrome
reports #term's height as the full 440px while its content box is 420px, so the
addon laid out ~12px more terminal than there was room for and
`.term-shell{overflow:hidden}` cut it off. At a small font size a whole line was
lost.

Measured after the fix, overflow at every font size from 9 to 26: <= 0px.

These are source assertions, since the browser measurement is not something a
unit test can run - but they fail loudly if the combination that caused the bug
is reintroduced.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = (ROOT / "web" / "app.css").read_text()
JS = (ROOT / "web" / "js" / "terminal.js").read_text()

TERM_RULE = re.search(r"^#term\{[^}]*\}", CSS, re.M).group(0)


def test_the_terminal_box_is_content_box():
    """The whole fix. With border-box the declared height includes the padding
    the addon cannot see."""
    assert "box-sizing:content-box" in TERM_RULE


def test_the_declared_height_is_the_usable_height():
    """Under content-box the height must be the inner height, so the addon and
    the box agree on the same number."""
    assert re.search(r"height:420px", TERM_RULE)


def test_the_global_reset_is_still_border_box():
    """This test only means anything while #term is the exception."""
    assert "*{box-sizing:border-box}" in CSS


def test_the_shell_still_clips():
    """overflow:hidden is what made the miscalculation visible, and it is also
    what keeps the rounded corners. It stays; the arithmetic is what changed."""
    shell = re.search(r"^\.term-shell\{[^}]*\}", CSS, re.M).group(0)
    assert "overflow:hidden" in shell


def test_fitting_is_deferred_after_a_font_change():
    """Changing fontSize makes xterm re-measure its cell size; fitting in the
    same tick can use the old metrics."""
    block = JS[JS.index("const setFont = (d)"):]
    block = block[:block.index("labels();", 10)]
    assert "requestAnimationFrame(refit)" in block
    assert re.search(r"fontSize = next;\s*refit\(\)", block) is None


def test_fitting_is_repeated_after_the_terminal_opens():
    """The first fit runs before the browser has necessarily resolved the
    monospace font's metrics."""
    block = JS[JS.index("function open()"):]
    assert "requestAnimationFrame(refit)" in block


def test_fullscreen_still_flexes_rather_than_fixing_a_height():
    assert re.search(r"\.term-shell\.full #term\{[^}]*flex:1", CSS)
    assert re.search(r"\.term-shell\.full #term\{[^}]*min-height:0", CSS)
