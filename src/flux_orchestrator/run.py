"""One flux-metering run: find the pointing where most light reaches the fiber, and measure how far
that is, in imager pixels, from where acquisition put the star.

The unit acquires, puts the folding mirror in and walks PWI4's spiral, saving an imager frame at
every cell. At each cell this takes `number_of_frames` flux frames from the flux-meter service and
reduces them to a median. The ring rule decides when to stop. The answer is the shift between the
spiral's reference frame and the frame at the arg-max cell, both read from the share.

Ported from MAST_unit `src/flux_metering/session.py` at `9fc7be6`; design in
`mast-claude-config/plans/flux_metering_design.md`. Saturation is recorded, never acted on.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
from astropy.io import fits

from flux_meter_service.models import CameraInfo, Settings
from flux_meter_service.save import observing_night
from flux_orchestrator.correlate import measure_pair
from flux_orchestrator.flux_client import US_PER_S, FluxFrame, FluxMeterClient
from flux_orchestrator.frame_shift import MIN_CONFIDENCE
from flux_orchestrator.photometry import FluxMeasurement, measure_frame, saturated_pixels
from flux_orchestrator.spiral import RingRule, Terminal, radius_arcsec, representative_of
from flux_orchestrator.unit_client import Target, UnitClient

logger = logging.getLogger(__name__)

#: Where the unit's products appear on mast-ns-spec: the share's MAST root, which the unit's
#: reported paths are relative to.
DEFAULT_SHARE_ROOT = Path(r"\\mast-ns-control\mast-share\MAST")
#: Beside the flux-meter service's own frames, under the share's tree for this host.
DEFAULT_OUTPUT_ROOT = Path(r"\\mast-ns-control\mast-share\MAST\mast-ns-spec")
RUN_FOLDER = "flux-orchestrator"
RESULT_FILE = "result.json"
SEQUENCE_DIGITS = 4
_RUN_PATTERN = re.compile(r"^\d+$")

#: The move to the share is queued on the unit, so a frame arrives some time after its step.
FRAME_ARRIVAL_TIMEOUT_S = 180.0
FRAME_POLL_INTERVAL_S = 1.0

#: Counted inside the aperture: a clipped fiber core shows tens of pixels at the rail, a cosmic
#: ray or hot pixel one or two.
SATURATED_PIXELS_ALLOWED = 2

#: PHD2's header cards on the imager frames.
DEC_CARD = "DEC"
PIXEL_SCALE_CARD = "PIXSCALE"


@dataclass(frozen=True)
class RunParams:
    unit: str
    seconds: float = 5.0
    target: Target = field(default_factory=Target)
    gain: int | None = None
    x_step_arcsec: float = 0.5
    y_step_arcsec: float = 0.5
    max_rings: int = 6
    patience_rings: int = 1
    max_radius_arcsec: float = 10.0
    flux_gain: int = 0
    flux_black_level: int = 3
    number_of_frames: int = 3
    usable_fraction: float = 0.66
    #: The fiber's position on the imager, for the correlation. None: the unit's configured
    #: `guiding.rois[fcu_v2]`, then the frame center, as `spiral_new_path` resolves it.
    center: tuple[int, int] | None = None
    #: Engineering only: walk the spiral wherever the mount points. Such a run's dx/dy is not a
    #: fiber-position measurement, and the flag is echoed into the result so it cannot be
    #: mistaken for one.
    skip_acquisition: bool = False

    @property
    def flux_exposure_us(self) -> int:
        """The flux exposure follows the imager's: a millisecond exposure samples one instant of
        a twinkling star, putting scintillation on the curve exactly where it is flattest."""
        return round(self.seconds * US_PER_S)


@dataclass
class FluxExposure:
    flux_frame: str
    requested_utc: str
    received_utc: str
    measurement: FluxMeasurement
    saturated_pixels: int
    saturated: bool


@dataclass
class Step:
    index: int
    cell: tuple[int, int]
    ring: int
    offset_arcsec: tuple[float, float]
    #: Relative to the share's MAST root; None with `imager_error` when the unit saved none.
    imager_frame: str | None
    imager_error: str | None
    flux: float | None
    representative: int
    saturated: bool
    exposures: list[FluxExposure]


@dataclass
class Result:
    dx: float
    dy: float
    confidence: float
    at_origin: bool
    low_confidence: bool
    magnitude_px: float
    max_reliable_shift_px: float
    beyond_limit: bool
    fiber_x: int
    fiber_y: int
    fiber_source: str
    proposed_fiber_x: float
    proposed_fiber_y: float
    argmax_index: int
    argmax_cell: tuple[int, int]
    argmax_ring: int
    argmax_frame: str
    argmax_offset_arcsec: tuple[float, float]
    argmax_saturated: bool
    #: The arg-max cell's commanded offset in pixels, which (dx, dy) should match in sign and
    #: magnitude; None when the frame carries no plate scale.
    commanded_offset_px: tuple[float, float] | None
    pixel_scale_arcsec: float | None
    dec_degrees: float | None


@dataclass
class RunRecord:
    params: RunParams
    folder: str
    started_at: str
    camera: CameraInfo
    ended_at: str | None = None
    terminal: Terminal | None = None
    error: str | None = None
    session: dict[str, Any] | None = None
    unit_end_path: dict[str, Any] | None = None
    steps: list[Step] = field(default_factory=list)
    best_index: int | None = None
    result: Result | None = None
    no_result_reason: str | None = None


def _iso_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def make_run_folder(output_root: Path, now: datetime.datetime) -> Path:
    """`<output root>/<observing night>/flux-orchestrator/NNNN`, one past the highest already there."""
    parent = output_root / observing_night(now) / RUN_FOLDER
    parent.mkdir(parents=True, exist_ok=True)
    numbers = [int(p.name) for p in parent.iterdir() if p.is_dir() and _RUN_PATTERN.match(p.name)]
    folder = parent / f"{max(numbers, default=0) + 1:0{SEQUENCE_DIGITS}d}"
    folder.mkdir()
    return folder


def on_share(share_root: Path, relative: str) -> Path:
    """The unit reports paths relative to the share's MAST root, always with forward slashes."""
    return share_root.joinpath(*PurePosixPath(relative).parts)


def read_frame(path: Path) -> tuple[fits.Header, np.ndarray]:
    """Read a frame the unit has queued for the share, waiting for it to arrive whole.

    A file that exists but will not read is still being copied, so both are waited out; one
    that never arrives is a lost frame, and that is the error.
    """
    deadline = time.monotonic() + FRAME_ARRIVAL_TIMEOUT_S
    while True:
        try:
            data = np.asarray(fits.getdata(path, memmap=False), dtype=float)
            return fits.getheader(path), data
        except (OSError, ValueError) as ex:
            if time.monotonic() > deadline:
                raise FileNotFoundError(f"'{path}' did not arrive within {FRAME_ARRIVAL_TIMEOUT_S:g} s: {ex}") from ex
            time.sleep(FRAME_POLL_INTERVAL_S)


def numeric_card(header: fits.Header, key: str) -> float | None:
    value = header.get(key)
    return float(value) if isinstance(value, int | float) else None


class Run:
    def __init__(
        self,
        params: RunParams,
        unit: UnitClient,
        meter: FluxMeterClient,
        share_root: Path = DEFAULT_SHARE_ROOT,
        output_root: Path = DEFAULT_OUTPUT_ROOT,
    ) -> None:
        self.params = params
        self.unit = unit
        self.meter = meter
        self.share_root = share_root
        # Before the folder: a service that is not there should not leave an empty run behind.
        self.camera = meter.info()
        self.folder = make_run_folder(output_root, datetime.datetime.now(datetime.UTC))
        self.record = RunRecord(params=params, folder=str(self.folder), started_at=_iso_now(), camera=self.camera)
        self._session_open = False
        self._last_aperture_position: tuple[float, float] | None = None
        self._dec_degrees: float | None = None
        self._pixel_scale: float | None = None

    def run(self) -> RunRecord:
        """The whole run. Always writes `result.json`; re-raises what ended a failed run."""
        try:
            self.record.terminal = self._run()
        except KeyboardInterrupt:
            self.record.terminal = Terminal.ABORTED
            if not self._session_open:
                self.unit.abort()
        except Exception as ex:
            self.record.terminal = Terminal.FAILED
            self.record.error = f"{type(ex).__name__}: {ex}"
            raise
        finally:
            if self._session_open:
                self._end_session()
            self.record.ended_at = _iso_now()
            self._write_record()
        return self.record

    def _run(self) -> Terminal:
        p = self.params
        self.meter.configure(Settings(exposure_us=p.flux_exposure_us, gain=p.flux_gain, black_level=p.flux_black_level))

        if p.skip_acquisition:
            logger.warning("acquisition SKIPPED: this run is a shakedown, and its dx/dy is not a fiber measurement")
        else:
            logger.info(f"{p.unit}: acquiring")
            self.unit.acquire(p.target, seconds=p.seconds, gain=p.gain)
        # After the acquisition: it takes the mirror out to solve and puts it back.
        self.unit.put_mirror_in()

        session = self.unit.spiral_new_path(p.x_step_arcsec, p.y_step_arcsec, p.seconds, p.usable_fraction, p.center)
        self._session_open = True
        self.record.session = session
        reference = f"{session['shared_folder']}/{session['reference_image']}"
        header, _data = read_frame(on_share(self.share_root, reference))
        self._dec_degrees = numeric_card(header, DEC_CARD)
        self._pixel_scale = numeric_card(header, PIXEL_SCALE_CARD)

        terminal = self._walk(reference)
        self._end_session()
        self._measure(reference)
        return terminal

    def _walk(self, reference: str) -> Terminal:
        p = self.params
        rule = RingRule(patience_rings=p.patience_rings, max_rings=p.max_rings)
        # The origin's frame is the reference itself: the spiral has not moved yet.
        cell, ring, image, image_error = (0, 0), 0, reference, None
        index = 0
        while True:
            step = self._measure_step(index, cell, ring, image, image_error)
            self.record.steps.append(step)
            if step.flux is not None and (rule.best_flux is None or step.flux > rule.best_flux):
                self.record.best_index = index
            terminal = rule.update(step.flux, ring)
            if terminal is not None:
                return terminal
            if radius_arcsec(cell, p.x_step_arcsec, p.y_step_arcsec, self._dec_degrees or 0.0) > p.max_radius_arcsec:
                return Terminal.MAX_RADIUS

            moved = self.unit.spiral_next_step(p.seconds)
            cell, ring, image, image_error = moved.cell, moved.ring, moved.image, moved.image_error
            index += 1

    def _measure_step(
        self, index: int, cell: tuple[int, int], ring: int, image: str | None, image_error: str | None
    ) -> Step:
        p = self.params
        exposures = [self._flux_exposure(index, n, cell, ring) for n in range(p.number_of_frames)]
        flux, representative = representative_of([e.measurement.net_counts for e in exposures])
        shown = f"{flux:.0f}" if flux is not None else "unmeasurable"
        logger.info(f"step {index}: cell={cell} ring={ring} flux={shown} (median of {len(exposures)})")
        return Step(
            index=index,
            cell=cell,
            ring=ring,
            offset_arcsec=(cell[0] * p.x_step_arcsec, cell[1] * p.y_step_arcsec),
            imager_frame=image,
            imager_error=image_error,
            flux=flux,
            representative=representative,
            saturated=exposures[representative].saturated,
            exposures=exposures,
        )

    def _flux_exposure(self, index: int, n: int, cell: tuple[int, int], ring: int) -> FluxExposure:
        frame = self.meter.expose()
        name = f"flux-{index:05d}-{n:02d}.fits"
        self._write_flux_frame(name, frame, index, cell, ring)
        # Centered on the source it finds; when it finds none, re-measured where the last one was
        # found, so every step is compared through the same window.
        measurement = measure_frame(frame.data, last_position=self._last_aperture_position)
        if measurement.position_source == "detected" and measurement.x is not None and measurement.y is not None:
            self._last_aperture_position = (measurement.x, measurement.y)
        return FluxExposure(
            flux_frame=name,
            requested_utc=frame.requested.isoformat(),
            received_utc=frame.received.isoformat(),
            measurement=measurement,
            saturated_pixels=saturated_pixels(frame.data, self.camera.saturation_level),
            saturated=measurement.saturated_in_aperture > SATURATED_PIXELS_ALLOWED,
        )

    def _end_session(self) -> None:
        """Close the unit's spiral session. It stops tracking; its own correlation is of the last
        cell, kept for the record only."""
        self._session_open = False
        self.record.unit_end_path = self.unit.spiral_end_path(self.params.seconds)

    def _measure(self, reference: str) -> None:
        if self.record.best_index is None:
            self.record.no_result_reason = "no step could be measured, so there is no arg-max"
            return
        best = self.record.steps[self.record.best_index]
        if best.imager_frame is None:
            self.record.no_result_reason = f"the arg-max step {best.index} has no imager frame: {best.imager_error}"
            return

        _header, reference_data = read_frame(on_share(self.share_root, reference))
        _header, final = read_frame(on_share(self.share_root, best.imager_frame))

        center_x, center_y, center_source = self._center(reference_data.shape)
        shift, magnitude, limit = measure_pair(
            reference_data,
            final,
            center_x=center_x,
            center_y=center_y,
            usable_fraction=self.params.usable_fraction,
            # The reference is the origin's frame, so an arg-max there is a null measurement.
            expect_no_motion=best.cell == (0, 0),
        )
        self.record.result = Result(
            dx=shift.dx,
            dy=shift.dy,
            confidence=shift.confidence,
            at_origin=shift.at_origin,
            low_confidence=shift.confidence < MIN_CONFIDENCE,
            magnitude_px=magnitude,
            max_reliable_shift_px=limit,
            beyond_limit=magnitude > limit,
            fiber_x=center_x,
            fiber_y=center_y,
            fiber_source=center_source,
            proposed_fiber_x=center_x + shift.dx,
            proposed_fiber_y=center_y + shift.dy,
            argmax_index=best.index,
            argmax_cell=best.cell,
            argmax_ring=best.ring,
            argmax_frame=best.imager_frame,
            argmax_offset_arcsec=best.offset_arcsec,
            argmax_saturated=best.saturated,
            commanded_offset_px=self._commanded_offset_px(best.offset_arcsec),
            pixel_scale_arcsec=self._pixel_scale,
            dec_degrees=self._dec_degrees,
        )

    def _center(self, shape: tuple[int, ...]) -> tuple[int, int, str]:
        """The correlation's center, as the unit resolved it for its own session."""
        end = self.record.unit_end_path or {}
        if end.get("center_x") is not None and end.get("center_y") is not None:
            return int(end["center_x"]), int(end["center_y"]), str(end.get("center_source"))
        return shape[1] // 2, shape[0] // 2, "frame centre"

    def _commanded_offset_px(self, offset_arcsec: tuple[float, float]) -> tuple[float, float] | None:
        if not self._pixel_scale or self._pixel_scale <= 0:
            return None
        ra_scale = math.cos(math.radians(self._dec_degrees)) if self._dec_degrees is not None else 1.0
        return (offset_arcsec[0] * ra_scale / self._pixel_scale, offset_arcsec[1] / self._pixel_scale)

    def _write_flux_frame(self, name: str, frame: FluxFrame, index: int, cell: tuple[int, int], ring: int) -> None:
        p, camera = self.params, self.camera
        header = fits.Header()
        header["DATE-OBS"] = (frame.requested.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], "UTC, exposure requested")
        header["DATE-END"] = (frame.received.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3], "UTC, frame received")
        header["EXPTIME"] = (p.flux_exposure_us / US_PER_S, "[s]")
        header["GAIN"] = p.flux_gain
        header["BLKLEVEL"] = (p.flux_black_level, "black level commanded on the camera")
        header["INSTRUME"] = (camera.model, "the flux meter")
        header["CAMSN"] = (camera.serial_number, "flux meter serial number")
        header["SATURATE"] = (camera.saturation_level, "[counts] full scale")
        header["TELESCOP"] = (p.unit, "the unit")
        header["STEP"] = index
        header["CELLX"] = (cell[0], "spiral cell, RA steps")
        header["CELLY"] = (cell[1], "spiral cell, Dec steps")
        header["RING"] = ring
        header["CREATOR"] = "flux-orchestrator"
        fits.PrimaryHDU(data=frame.data, header=header).writeto(self.folder / name)

    def _write_record(self) -> None:
        path = self.folder / RESULT_FILE
        path.write_text(json.dumps(asdict(self.record), indent=2, default=str), encoding="utf-8")
        logger.info(f"wrote {path}")
