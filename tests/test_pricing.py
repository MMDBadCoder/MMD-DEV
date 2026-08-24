"""Pricing arithmetic. Every number a customer is charged originates here, so
these are the tests that matter most."""
import pytest

from mmd.billing import pricing as P
from mmd.billing.pricing import MICRO, Rates, Tier

R = Rates.from_settings()


def T(cpu=1000, mem=1024, disk=10):
    return Tier(cpu, mem, disk)


# --- tier validation ---------------------------------------------------
@pytest.mark.parametrize("cpu", P.CPU_OPTIONS_MILLI)
def test_every_offered_cpu_is_accepted(cpu):
    assert Tier(cpu, 1024, 10).cpu_milli == cpu


@pytest.mark.parametrize("mem", P.MEM_OPTIONS_MIB)
def test_every_offered_memory_is_accepted(mem):
    assert Tier(1000, mem, 10).mem_mib == mem


@pytest.mark.parametrize("cpu", [0, 1, 200, 250, 750, 1500, 2500, 4000, 100000, -500])
def test_cpu_outside_the_catalogue_is_refused(cpu):
    with pytest.raises(P.InvalidTier):
        Tier(cpu, 1024, 10)


@pytest.mark.parametrize("mem", [0, 100, 500, 768, 1500, 2000, 7168, 999999, -1024])
def test_memory_outside_the_catalogue_is_refused(mem):
    with pytest.raises(P.InvalidTier):
        Tier(1000, mem, 10)


def test_the_200_millicore_case_from_the_brief_is_refused():
    # Named explicitly because it is the example that prompted the rule.
    with pytest.raises(P.InvalidTier) as e:
        Tier(200, 1024, 10)
    assert "0.2" in str(e.value)


def test_refusal_names_the_available_choices():
    with pytest.raises(P.InvalidTier) as e:
        Tier(1500, 1024, 10)
    for opt in ("0.5", "1", "2", "3"):
        assert opt in str(e.value)


def test_zero_or_negative_disk_is_refused():
    for d in (0, -1):
        with pytest.raises(P.InvalidTier):
            Tier(1000, 1024, d)


def test_parse_tier_rejects_non_numeric():
    with pytest.raises(P.InvalidTier):
        P.parse_tier("abc", 1024, 10)


# --- derived views -----------------------------------------------------
def test_millicores_convert_to_cores():
    assert Tier(500, 1024, 10).cpu_cores == 0.5
    assert Tier(3000, 1024, 10).cpu_cores == 3.0


def test_mib_converts_to_gib():
    assert Tier(1000, 512, 10).mem_gib == 0.5
    assert Tier(1000, 6144, 10).mem_gib == 6.0


def test_comfort_threshold_is_2gb():
    assert not Tier(1000, 1024, 10).is_comfortable
    assert Tier(1000, 2048, 10).is_comfortable


# --- translation into Incus limits -------------------------------------
def test_half_core_becomes_a_50ms_quota():
    cfg = Tier(500, 1024, 10).incus_config()
    assert cfg["limits.cpu.allowance"] == "50ms/100ms"


def test_three_cores_become_a_300ms_quota():
    assert Tier(3000, 1024, 10).incus_config()["limits.cpu.allowance"] == "300ms/100ms"


def test_fractional_cpu_still_exposes_a_whole_core():
    # The workspace must SEE a core to run anything at all; the quota is what
    # actually caps it.
    assert Tier(500, 1024, 10).incus_config()["limits.cpu"] == "1"


def test_memory_limit_is_expressed_in_mib():
    assert Tier(1000, 2048, 10).incus_config()["limits.memory"] == "2048MiB"


# --- cost components ---------------------------------------------------
def test_disk_costs_the_same_running_or_stopped():
    t = T()
    assert P.disk_micro(t, R) == P.off_hour_micro(t, R)


def test_switched_off_costs_only_disk():
    """A stopped workspace still holds its full ZFS refreservation, so the space
    is genuinely unavailable to anyone else. Nothing else accrues."""
    t = T()
    assert P.off_hour_micro(t, R) == P.disk_micro(t, R)


def test_reservation_scales_with_both_cpu_and_memory():
    small = P.reservation_micro(T(500, 512), R)
    big = P.reservation_micro(T(3000, 6144), R)
    assert big > small * 4


def test_usage_never_goes_negative():
    # A counter reset must not become a refund.
    assert P.usage_micro(-5.0, -3.0, R) == 0


def test_partial_hour_is_proportional():
    t = T()
    full = P.reservation_micro(t, R)
    half = P.reservation_micro(t, R, fraction=0.5)
    assert half == pytest.approx(full / 2, rel=1e-9)


# --- the gate ----------------------------------------------------------
def test_gate_is_the_most_an_hour_can_cost():
    t = T()
    flat_out, _ = P.settle_micro(t, R, powered_on=True,
                                 cpu_core_hours=t.cpu_cores,
                                 mem_gib_hours=t.mem_gib)
    assert P.max_hour_micro(t, R) == flat_out


def test_gate_exceeds_an_idle_hour():
    t = T()
    assert P.max_hour_micro(t, R) > P.idle_hour_micro(t, R)


def test_idle_hour_exceeds_a_stopped_hour():
    t = T()
    assert P.idle_hour_micro(t, R) > P.off_hour_micro(t, R)


def test_publishing_a_port_is_free():
    """It hands out an nftables DNAT rule and a number from a range of 10,000 -
    neither scarce enough to meter. The gate must not move when a customer
    publishes one, and no rate may exist to make it move."""
    t = T()
    assert P.max_hour_micro(t, R) == (P.disk_micro(t, R)
                                      + P.reservation_micro(t, R)
                                      + P.usage_micro(t.cpu_cores, t.mem_gib, R))
    assert not hasattr(R, "port")
    assert not hasattr(P, "ports_micro")
    assert not any("port" in k for k in P.DEFAULT_RATES)
    assert not any("port" in k for k in R.as_dict())


def test_bigger_tiers_cost_more():
    seen = [P.max_hour_micro(Tier(c, m, 10), R)
            for c, m in zip(P.CPU_OPTIONS_MILLI, (512, 1024, 2048, 6144))]
    assert seen == sorted(seen)


# --- settlement --------------------------------------------------------
def test_stopped_settlement_omits_cpu_and_memory():
    _, d = P.settle_micro(T(), R, powered_on=False)
    assert d["reservation"] == 0 and d["usage"] == 0
    assert d["disk"] > 0


def test_running_settlement_includes_every_component():
    _, d = P.settle_micro(T(), R, powered_on=True,
                          cpu_core_hours=0.4, mem_gib_hours=0.7)
    for k in ("disk", "reservation", "usage"):
        assert d[k] > 0
    assert "ports" not in d


def test_archived_settlement_uses_the_reduced_disk_rate():
    t = T()
    live, _ = P.settle_micro(t, R, powered_on=False)
    archived, d = P.settle_micro(t, R, powered_on=False, archived=True)
    assert archived < live
    assert d["archived"] is True


def test_settlement_records_the_tier_it_charged():
    # The ledger must say which size was billed, or a mid-hour resize becomes
    # impossible to audit after the fact.
    _, d = P.settle_micro(T(2000, 2048), R, powered_on=True)
    assert d["tier"]["cpu_milli"] == 2000
    assert d["tier"]["mem_mib"] == 2048


def test_a_resize_cannot_repay_the_earlier_part_of_the_hour():
    """Run big for most of an hour, then shrink: the elapsed part must still be
    charged at the big size. This is the hole that closing mid-period
    settlement was for."""
    big, small = T(3000, 6144), T(500, 512)
    elapsed_at_big, _ = P.settle_micro(big, R, powered_on=True, fraction=0.9)
    same_span_at_small, _ = P.settle_micro(small, R, powered_on=True, fraction=0.9)
    assert elapsed_at_big > same_span_at_small * 3


# --- money -------------------------------------------------------------
def test_prices_are_in_toman():
    assert P.CURRENCY == "IRT"


def test_amounts_are_whole_micro_units():
    for cm in P.CPU_OPTIONS_MILLI:
        for mm in P.MEM_OPTIONS_MIB:
            v = P.max_hour_micro(Tier(cm, mm, 10), R)
            assert isinstance(v, int)


def test_default_tier_costs_a_sane_amount_per_hour():
    # A guard against a mis-scaled rate card: a 1c/1G machine should be tens to
    # low hundreds of Toman per hour, not fractions and not millions.
    toman = P.max_hour_micro(T(), R) / MICRO
    assert 100 <= toman <= 5000


def test_quote_itemises_and_totals_consistently():
    q = P.quote(T(), R)
    ph = q["per_hour"]
    total = (ph["disk"] + ph["cpu_reservation"]
             + ph["mem_reservation"] + ph["cpu_usage_max"] + ph["mem_usage_max"])
    assert total == pytest.approx(q["max_per_hour"], rel=1e-6)


def test_rates_fall_back_to_defaults_when_settings_are_junk():
    r = Rates.from_settings({"rate_disk_per_gib_hour": "not-a-number"})
    assert r.disk == P.DEFAULT_RATES["rate_disk_per_gib_hour"]


def test_admin_can_override_a_rate():
    r = Rates.from_settings({"rate_disk_per_gib_hour": "9"})
    assert r.disk == 9.0


def test_catalogue_matches_the_accepted_options():
    c = P.catalogue()
    assert [x["milli"] for x in c["cpu"]] == list(P.CPU_OPTIONS_MILLI)
    assert [x["mib"] for x in c["memory"]] == list(P.MEM_OPTIONS_MIB)
