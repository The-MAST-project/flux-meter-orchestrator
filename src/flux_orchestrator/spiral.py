"""When to stop walking the spiral, and which exposure stands for a step.

Ported from MAST_unit `src/flux_metering/session.py` at `9fc7be6` (`_walk_spiral` and
`representative_of`). The unit owns the traversal: PWI4 decides the order of the cells, and the
ring here is READ from the cell the unit reports, `max(|x|, |y|)`, never assumed.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import StrEnum


class Terminal(StrEnum):
    """Why a walk ended. Only `CONVERGED` says the arg-max is a peak rather than the best of a
    truncated search."""

    CONVERGED = "converged"
    MAX_RINGS = "max_rings"
    MAX_RADIUS = "max_radius"
    ABORTED = "aborted"
    FAILED = "failed"


def ring_of(cell: tuple[int, int]) -> int:
    return max(abs(cell[0]), abs(cell[1]))


def radius_arcsec(cell: tuple[int, int], x_step_arcsec: float, y_step_arcsec: float, dec_degrees: float) -> float:
    """Sky angle from the origin. The x step is RA COORDINATE arcsec, so the sky moves that times
    cos(dec) along it (MAST_unit#136); without it the radius reads 25% high at dec +41."""
    return math.hypot(cell[0] * x_step_arcsec * math.cos(math.radians(dec_degrees)), cell[1] * y_step_arcsec)


@dataclass
class RingRule:
    """The stopping rule: a complete ring with no improvement.

    A square spiral circles the origin, so flux rises and falls on every ring and "it went up
    then down" stops at the first near-pass. A completed ring is the smallest statement about
    the neighborhood that means anything. A ring is complete when the reported ring first
    exceeds it.
    """

    patience_rings: int
    max_rings: int
    best_flux: float | None = None
    best_ring: int = 0
    current_ring: int = 0
    rings_without_improvement: int = 0

    def update(self, flux: float | None, ring: int) -> Terminal | None:
        """Take one measured step. Returns why to stop, or None to step again.

        A step whose flux is None is skipped, never compared: defaulting it to zero would make
        a dark cell the worst cell rather than an unknown one.
        """
        if flux is not None and (self.best_flux is None or flux > self.best_flux):
            self.best_flux, self.best_ring = flux, ring

        if ring > self.current_ring:
            completed_with_improvement = self.best_ring >= self.current_ring
            self.rings_without_improvement = 0 if completed_with_improvement else self.rings_without_improvement + 1
            self.current_ring = ring
            if self.rings_without_improvement >= self.patience_rings:
                return Terminal.CONVERGED
            if ring > self.max_rings:
                return Terminal.MAX_RINGS
        return None


def representative_of(fluxes: list[float | None]) -> tuple[float | None, int]:
    """The median flux, and which exposure is nearest it.

    Unmeasurable exposures are excluded rather than counted as zero. Median, because the arg-max
    is decided where the coupling curve is flattest, where one outlier has most leverage. With
    an odd count the median is itself a sample. With no measurable exposure the flux is None and
    the index is 0, so the caller still has a frame to name.
    """
    usable = [(i, f) for i, f in enumerate(fluxes) if f is not None]
    if not usable:
        return None, 0
    median = float(statistics.median([f for _i, f in usable]))
    nearest = min(usable, key=lambda pair: abs(pair[1] - median))[0]
    return median, nearest
