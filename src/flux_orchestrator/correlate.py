"""The shift between the reference frame and the arg-max step's frame.

Copied from MAST_unit `src/flux_metering/correlate.py` at `9fc7be6` (`measure_pair`); the unit's
copy goes when its flux metering is removed.
"""

from __future__ import annotations

import math

import numpy as np

from flux_orchestrator.frame_shift import ShiftResult, margins_from_fraction, max_reliable_shift, measure_shift


def measure_pair(
    reference: np.ndarray,
    final: np.ndarray,
    *,
    center_x: int,
    center_y: int,
    usable_fraction: float,
    expect_no_motion: bool,
) -> tuple[ShiftResult, float, float]:
    """Returns `(shift, magnitude_px, max_reliable_shift_px)`."""
    shape = (reference.shape[0], reference.shape[1])
    margin_h, margin_v = margins_from_fraction(shape, usable_fraction)
    shift = measure_shift(
        reference,
        final,
        center_x=center_x,
        center_y=center_y,
        margin_horizontal=margin_h,
        margin_vertical=margin_v,
        expect_no_motion=expect_no_motion,
    )
    return shift, math.hypot(shift.dx, shift.dy), max_reliable_shift(shape, margin_h)
