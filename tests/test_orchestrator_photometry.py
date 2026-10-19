"""The flux of one frame: the fiber is measured, darkness and spikes are not."""

from __future__ import annotations

import warnings

import numpy as np
import pytest

from flux_orchestrator.photometry import measure_frame

SHAPE = (256, 256)
SPOT_SIGMA_PX = 5.4


def frame(peak: float = 800.0, spike_at: tuple[int, int] | None = None) -> np.ndarray:
    rng = np.random.default_rng(0)
    yy, xx = np.mgrid[0 : SHAPE[0], 0 : SHAPE[1]]
    data = rng.normal(3.0, 1.0, SHAPE)
    data += peak * np.exp(-((xx - 128) ** 2 + (yy - 128) ** 2) / (2 * SPOT_SIGMA_PX**2))
    if spike_at is not None:
        data[spike_at] = 1000.0
    return np.clip(data, 0, 1023)


@pytest.fixture(autouse=True)
def quiet_photutils():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


def test_the_fiber_spot_is_detected_and_measured():
    measured = measure_frame(frame())
    assert measured.position_source == "detected"
    assert measured.x == pytest.approx(128, abs=0.5)
    expected = 800.0 * 2 * np.pi * SPOT_SIGMA_PX**2
    assert measured.net_counts == pytest.approx(expected, rel=0.05)


def test_a_dark_frame_with_no_previous_position_is_unmeasured_not_zero():
    measured = measure_frame(frame(peak=0.0))
    assert measured.net_counts is None
    assert measured.position_source == "none"


def test_a_dark_frame_is_measured_where_the_fiber_last_was():
    measured = measure_frame(frame(peak=0.0), last_position=(128.0, 128.0))
    assert measured.position_source == "inherited"
    assert measured.net_counts is not None and abs(measured.net_counts) < 500


def test_a_lone_hot_pixel_is_not_taken_for_the_fiber():
    measured = measure_frame(frame(peak=0.0, spike_at=(40, 200)))
    assert measured.position_source != "detected"
