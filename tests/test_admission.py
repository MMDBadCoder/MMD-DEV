"""Elastic capacity admission."""
import pytest

from mmd.scheduler.admission import AdmissionResult, Capacity, can_start


def cap(cores=4.0, mem=8.0, rc=1.0, rm=2.0, oc=2.0, om=1.0):
    return Capacity(cores, mem, rc, rm, oc, om)


def test_host_reserve_is_subtracted_before_overcommit():
    c = cap(cores=4.0, rc=1.0, oc=2.0)
    assert c.schedulable_cores == 6.0          # (4-1) * 2


def test_memory_is_not_oversubscribed_by_default():
    c = cap(mem=8.0, rm=2.0, om=1.0)
    assert c.schedulable_mem_gib == 6.0


def test_an_empty_host_admits_a_machine():
    assert can_start(cap(), [], 1.0, 1.0).allowed


def test_a_machine_that_fits_exactly_is_admitted():
    c = cap()
    assert can_start(c, [], c.schedulable_cores, c.schedulable_mem_gib).allowed


def test_one_byte_over_memory_is_refused():
    c = cap()
    r = can_start(c, [], 1.0, c.schedulable_mem_gib + 0.001)
    assert not r.allowed and r.resource == "memory"


def test_running_machines_consume_capacity():
    c = cap()
    running = [(1.0, 1.0)] * 5              # 5 GiB of 6 used
    assert not can_start(c, running, 1.0, 2.0).allowed
    assert can_start(c, running, 1.0, 1.0).allowed


def test_refusal_says_which_resource_ran_out():
    c = cap()
    assert can_start(c, [(6.0, 0.0)], 1.0, 1.0).resource == "cpu"
    assert can_start(c, [(0.0, 6.0)], 1.0, 1.0).resource == "memory"


def test_memory_is_checked_before_cpu():
    # Memory exhaustion is the one that causes OOM kills, so it is the more
    # useful thing to tell the customer about when both are short.
    c = cap()
    assert can_start(c, [(6.0, 6.0)], 1.0, 1.0).resource == "memory"


def test_a_refusal_reports_what_is_left():
    c = cap()
    r = can_start(c, [(1.0, 1.0)], 99.0, 1.0)
    assert not r.allowed and r.free_cores == pytest.approx(5.0)


def test_an_admitted_request_reports_what_remains_after_it():
    r = can_start(cap(), [], 1.0, 1.0)
    assert r.allowed and r.free_mem_gib == pytest.approx(5.0)


def test_reserve_larger_than_the_host_yields_no_capacity_rather_than_negative():
    c = cap(cores=1.0, mem=1.0, rc=4.0, rm=8.0)
    assert c.schedulable_cores == 0.0 and c.schedulable_mem_gib == 0.0
    assert not can_start(c, [], 0.5, 0.5).allowed


def test_default_host_capacity_reads_the_real_machine():
    from mmd.scheduler.admission import host_capacity
    c = host_capacity()
    assert c.total_cores >= 1 and c.total_mem_gib > 0.5
