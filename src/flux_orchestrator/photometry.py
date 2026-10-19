"""One flux frame reduced to the light that left the fiber.

Copied from MAST_unit `src/flux_metering/flux_meter.py` at `9fc7be6` (`measure_frame` and its
guards); the unit's copy goes when its flux metering is removed.
"""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass

import numpy as np
from photutils.utils.exceptions import NoDetectionsWarning

from flux_orchestrator.aperture_photometry_single import SATURATION_ADU, SPIKE_PEAK_FRACTION, measure_single_image

logger = logging.getLogger(__name__)

#: Below this FWHM a "detection" is not the fiber. The fiber output measures 12.4-13.3 px on
#: the real camera; a hot pixel or a cosmic ray measures about 1.1. On run 0006 the backend's
#: smoothed-peak fallback locked onto a hot pixel at (943, 162) and reported ~1338 counts as the
#: fiber, while another exposure of the same step measured the real thing at 92666.
MIN_PLAUSIBLE_FWHM_PX = 3.0


@dataclass
class FluxMeasurement:
    """One frame, reduced. `net_counts` is None when there was nothing to measure.

    None rather than 0.0: a frame the photometry could not measure and a frame that genuinely
    contains no light produce the same number under a plain sum, and the arg-max cannot tell
    them apart.
    """

    net_counts: float | None
    position_source: str
    counts_err: float | None = None
    snr: float | None = None
    x: float | None = None
    y: float | None = None
    radius_px: float | None = None
    bkg_level: float | None = None
    fwhm_px: float | None = None
    #: Inside the aperture, at `saturation_threshold`: the count that makes the measurement a
    #: lower limit.
    saturated_in_aperture: int = 0
    #: Anywhere in the frame. A diagnostic: a hot pixel announces itself here.
    saturated_in_frame: int = 0
    saturation_threshold: int | None = None
    error: str | None = None


def _why_not_the_fiber(data: np.ndarray, found: dict, spike_peak_fraction: float) -> str | None:
    """Why this detection is not the fiber output, or None if it plausibly is.

    **Width** catches a hot pixel. **Concentration** catches a spike on a clean frame, where
    `measure_fwhm` gives up and returns its configured guess (11 px), which passes any width
    floor: a Gaussian of FWHM f puts about 0.88/f^2 of its counts in the peak pixel, an
    isolated spike all of them.
    """
    fwhm = float(found["fwhm_pix"])
    x, y = float(found["x"]), float(found["y"])
    if fwhm < MIN_PLAUSIBLE_FWHM_PX:
        return (
            f"rejected a detection at ({x:.0f}, {y:.0f}) with FWHM {fwhm:.1f} px, below the "
            f"{MIN_PLAUSIBLE_FWHM_PX:g} px floor: too narrow to be the fiber"
        )

    net = float(found["net_counts"])
    if net <= 0:
        return None

    radius = float(found["radius_pix"])
    ny, nx = data.shape
    yy, xx = np.ogrid[0:ny, 0:nx]
    inside = (xx - x) ** 2 + (yy - y) ** 2 <= radius**2
    if not inside.any():
        return None
    peak = float(data[inside].max()) - float(found["bkg_level"])
    fraction = peak / net
    if fraction > spike_peak_fraction:
        return (
            f"rejected a detection at ({x:.0f}, {y:.0f}): {100 * fraction:.0f}% of its light is "
            f"in one pixel (limit {100 * spike_peak_fraction:.0f}%), so it is a spike, not the fiber"
        )
    return None


def measure_frame(frame: np.ndarray, last_position: tuple[float, float] | None = None) -> FluxMeasurement:
    """Aperture photometry on one flux frame. Never raises.

    - `detected`: the source was found in this frame.
    - `inherited`: nothing was found, so the frame was measured at `last_position`, through the
      same aperture as every other step, keeping the flux curve continuous.
    - `none`: nothing found and no previous position; `net_counts` is None.

    Most of a spiral is far from the peak, where the fiber is dark, so finding nothing is the
    common case rather than an error.
    """
    data = np.asarray(frame, dtype=float)
    in_frame = int(np.count_nonzero(data >= SATURATION_ADU))

    def reduced(result: dict, source: str) -> FluxMeasurement:
        return FluxMeasurement(
            net_counts=float(result["net_counts"]),
            position_source=source,
            counts_err=float(result["counts_err"]),
            snr=float(result["snr"]),
            x=float(result["x"]),
            y=float(result["y"]),
            radius_px=float(result["radius_pix"]),
            bkg_level=float(result["bkg_level"]),
            fwhm_px=float(result["fwhm_pix"]),
            saturated_in_aperture=int(result["n_saturated"]),
            saturated_in_frame=in_frame,
            saturation_threshold=SATURATION_ADU,
        )

    rejection: str | None = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NoDetectionsWarning)
            found = measure_single_image(data, verbose=False)
        rejection = _why_not_the_fiber(data, found, SPIKE_PEAK_FRACTION)
        if rejection is None:
            return reduced(found, "detected")
        logger.warning(rejection)
    except Exception as detection_failed:  # noqa: BLE001 -- a dark cell is not a run failure
        rejection = str(detection_failed)

    def unmeasured() -> FluxMeasurement:
        return FluxMeasurement(
            net_counts=None,
            position_source="none",
            saturated_in_frame=in_frame,
            saturation_threshold=SATURATION_ADU,
            error=rejection,
        )

    if last_position is None:
        return unmeasured()

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NoDetectionsWarning)
            inherited = reduced(measure_single_image(data, position=last_position, verbose=False), "inherited")
        inherited.error = rejection
        return inherited
    except Exception as ex:  # noqa: BLE001 -- reported in the measurement, not raised
        logger.warning(f"could not measure at the inherited position {last_position}: {ex}")
        rejection = str(ex)
        return unmeasured()


def saturated_pixels(frame: np.ndarray, saturation_level: int) -> int:
    """How many pixels are at or above full scale, anywhere in the frame."""
    return int(np.count_nonzero(np.asarray(frame) >= saturation_level))
