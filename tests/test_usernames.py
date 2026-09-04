"""Usernames are DNS labels.

A customer's Hermes dashboard lives at `hermes.<username>.mmd-ai.ir`, so a
username is part of a hostname. Anything that is not a legal label does not look
odd - it breaks the address.
"""
import pytest

from mmd import usernames as U


def test_a_plain_name_is_accepted():
    assert U.validate("ali") == "ali"
    assert U.validate("ali-hosseini2") == "ali-hosseini2"


def test_case_is_folded_so_one_host_cannot_be_two_accounts():
    """DNS is case-insensitive: Ali and ali are the same host."""
    assert U.validate("  ALI  ") == "ali"


@pytest.mark.parametrize("bad,code", [
    ("", "username_required"),
    ("ab", "username_short"),
    ("a" * 33, "username_long"),
    ("ali.hosseini", "username_charset"),   # a dot adds a subdomain level
    ("ali_h", "username_charset"),          # underscores are illegal in hostnames
    ("-ali", "username_charset"),
    ("ali-", "username_charset"),
    ("ali h", "username_charset"),
    ("علی", "username_charset"),
    ("12345", "username_numeric"),
    ("admin", "username_reserved"),
    ("hermes", "username_reserved"),
    ("www", "username_reserved"),
    ("ports", "username_reserved"),
    ("ssh", "username_reserved"),
])
def test_rejections_carry_a_translatable_code(bad, code):
    with pytest.raises(U.UsernameError) as e:
        U.validate(bad)
    assert e.value.code == code


def test_reserved_names_cannot_impersonate_the_platform():
    """Taking one back later means renaming a live customer's dashboard."""
    for name in ("admin", "billing", "support", "console", "openrouter"):
        with pytest.raises(U.UsernameError):
            U.validate(name)


def test_names_are_deduplicated_for_migrations():
    taken = set()
    got = []
    for candidate in ("ali", "ali", "ali"):
        n = U.make_unique(candidate, taken)
        taken.add(n)
        got.append(n)
    assert got == ["ali", "ali-2", "ali-3"]
    assert len(set(got)) == 3


def test_a_deduplicated_name_still_fits_the_label_limit():
    long = "x" * U.MAX_LEN
    assert len(U.make_unique(long, {long})) <= U.MAX_LEN


def test_the_hostname_is_built_from_the_normalised_name():
    assert U.hermes_host("Ali", "mmd-ai.ir") == "hermes.ali.mmd-ai.ir"


def test_every_valid_username_makes_a_legal_hostname():
    import re
    label = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    for name in ("ali", "a-b-c", "x1", "ab2", "seyfoori-amir-h"):
        try:
            u = U.validate(name)
        except U.UsernameError:
            continue
        host = U.hermes_host(u, "mmd-ai.ir")
        assert all(label.match(part) for part in host.split("."))
