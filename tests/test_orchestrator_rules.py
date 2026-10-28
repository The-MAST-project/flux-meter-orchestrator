"""The stopping rule, the median choice and the sky radius, without any I/O."""

from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from flux_orchestrator.spiral import RingRule, Terminal, radius_arcsec, representative_of, ring_of


def walk(rule: RingRule, steps: Sequence[tuple[float | None, int]]) -> tuple[Terminal | None, int]:
    """Feed (flux, ring) pairs until the rule stops; returns why and after how many."""
    for n, (flux, ring) in enumerate(steps, start=1):
        terminal = rule.update(n - 1, flux, ring)
        if terminal is not None:
            return terminal, n
    return None, len(steps)


class TestRingRule:
    def test_a_complete_ring_without_improvement_converges(self):
        rule = RingRule(patience_rings=1, max_rings=6)
        # Peak in ring 1; ring 2 adds nothing; the first cell of ring 3 completes ring 2.
        steps = [(10.0, 0)] + [(50.0, 1)] + [(20.0, 1)] * 7 + [(5.0, 2)] * 16 + [(1.0, 3)]
        assert walk(rule, steps) == (Terminal.CONVERGED, len(steps))

    def test_rising_and_falling_inside_a_ring_does_not_stop_it(self):
        """A square spiral circles the origin, so flux goes up and down on every ring."""
        rule = RingRule(patience_rings=1, max_rings=6)
        steps = [(10.0, 0), (30.0, 1), (5.0, 1), (40.0, 1), (2.0, 1)]
        assert walk(rule, steps) == (None, len(steps))

    def test_an_improving_ring_resets_the_patience(self):
        rule = RingRule(patience_rings=2, max_rings=6)
        steps = [(10.0, 0), (5.0, 1), (60.0, 2), (1.0, 3), (1.0, 4), (1.0, 5)]
        assert walk(rule, steps) == (Terminal.CONVERGED, 6)

    def test_max_rings_stops_a_search_that_keeps_improving(self):
        rule = RingRule(patience_rings=1, max_rings=2)
        steps = [(1.0, 0), (2.0, 1), (3.0, 2), (4.0, 3)]
        assert walk(rule, steps) == (Terminal.MAX_RINGS, 4)

    def test_an_unmeasurable_step_is_never_the_best(self):
        rule = RingRule(patience_rings=1, max_rings=6)
        rule.update(0, None, 0)
        assert rule.best_index is None
        rule.update(1, -3.0, 1)
        assert (rule.best_index, rule.best_flux) == (1, -3.0), "a negative net count is a measurement; None is not"

    def test_a_step_with_no_cell_can_be_the_best_but_completes_no_ring(self):
        rule = RingRule(patience_rings=1, max_rings=6)
        rule.update(0, 10.0, 0)
        assert rule.update(1, 99.0, None) is None
        assert rule.best_index == 1
        assert rule.current_ring == 0, "an unknown cell says nothing about which ring is complete"


class TestRepresentative:
    def test_the_median_is_a_sample_with_an_odd_count(self):
        assert representative_of([5.0, 100.0, 7.0]) == (7.0, 2)

    def test_unmeasurable_exposures_are_left_out_not_counted_as_zero(self):
        assert representative_of([None, 9.0, 11.0, None, 10.0]) == (10.0, 4)

    def test_nothing_measurable_has_no_flux(self):
        assert representative_of([None, None]) == (None, 0)


def test_ring_is_read_from_the_cell():
    assert [ring_of(c) for c in [(0, 0), (1, -1), (-2, 1), (0, 3)]] == [0, 1, 2, 3]


def test_the_radius_is_the_offset_on_the_sky():
    assert radius_arcsec((2, 0), 1.0, 1.0) == pytest.approx(2.0)
    assert radius_arcsec((1, 1), 3.0, 4.0) == pytest.approx(5.0)
    assert math.isclose(radius_arcsec((0, 0), 1.0, 1.0), 0.0)
