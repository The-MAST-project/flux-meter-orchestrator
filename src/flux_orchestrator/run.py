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
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
from astropy.io import fits

from flux_meter_service.models import Settings
from flux_meter_service.save import observing_night
from flux_orchestrator.correlate import measure_pair
from flux_orchestrator.flux_client import US_PER_S, FluxFrame, FluxMeterClient
from flux_orchestrator.frame_shift import MIN_CONFIDENCE
from flux_orchestrator.photometry import FluxMeasurement, PositionSource, measure_frame, saturated_pixels
from flux_orchestrator.spiral import RingRule, Terminal, radius_arcsec, representative_of
from flux_orchestrator.unit_client import SpiralStep, Target, UnitApiError, UnitClient

logger = logging.getLogger(__name__)

#: What `spiral_new_path` reports once the unit takes its steps as angles on the sky (MAST_unit#136).
RA_STEP_COMMANDED = "ra_step_commanded_arcsec"
#: Where the unit's products appear on mast-ns-spec: the share's MAST root, which the unit's
#: reported paths are relative to.
DEFAULT_SHARE_ROOT = Path(r"\\mast-ns-control\mast-share\MAST")
#: Beside the flux-meter service's own frames, under the share's tree for this host.
DEFAULT_OUTPUT_ROOT = Path(r"\\mast-ns-control\mast-share\MAST\mast-ns-spec")
RUN_FOLDER = "flux-orchestrator"
RESULT_FILE = "result.json"
#: What the unit's spiral session writes beside its frames, on every ending.
UNIT_SESSION_RESULT = "result.json"
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


class Stage(StrEnum):
    """What the unit is doing for the run, which decides how an interruption leaves it."""

    PREPARING = "preparing"  # acquiring, or moving the mirror: abort the unit
    SPIRAL = "spiral"  # a session is requested or open: close it, which stops tracking
    MEASURING = "measuring"  # the session is closed: the unit is no longer the run's


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
    #: None when PWI4 reported no spiral offset for the step (`cell_error` says so).
    cell: tuple[int, int] | None
    ring: int | None
    offset_arcsec: tuple[float, float] | None
    cell_error: str | None
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
    #: The arg-max is the spiral's origin, whose frame is the reference itself: the brightest
    #: pointing is where acquisition put the star, so the shift is zero by definition and
    #: nothing is correlated. The correlation's own fields are then None.
    argmax_at_reference: bool
    confidence: float | None
    at_origin: bool | None
    low_confidence: bool
    magnitude_px: float
    max_reliable_shift_px: float | None
    beyond_limit: bool
    fiber_x: int
    fiber_y: int
    fiber_source: str
    proposed_fiber_x: float
    proposed_fiber_y: float
    argmax_index: int
    argmax_cell: tuple[int, int] | None
    argmax_ring: int | None
    argmax_frame: str
    argmax_offset_arcsec: tuple[float, float] | None
    argmax_saturated: bool
    #: The arg-max cell's commanded offset in pixels, which (dx, dy) should match in sign and
    #: magnitude; None when the frame carries no plate scale or the cell is unknown.
    commanded_offset_px: tuple[float, float] | None
    pixel_scale_arcsec: float | None
    dec_degrees: float | None


@dataclass
class RunRecord:
    params: RunParams
    folder: str
    started_at: str
    camera: dict[str, Any]
    ended_at: str | None = None
    terminal: Terminal | None = None
    error: str | None = None
    session: dict[str, Any] | None = None
    unit_end_path: dict[str, Any] | None = None
    unit_end_path_error: str | None = None
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


def read_arriving[T](path: Path, read: Callable[[Path], T]) -> T:
    """Read a file the unit has queued for the share, waiting for it to arrive whole.

    A file that exists but will not read is still being copied, so both are waited out; one
    that never arrives is lost, and that is the error.
    """
    deadline = time.monotonic() + FRAME_ARRIVAL_TIMEOUT_S
    while True:
        try:
            return read(path)
        except (OSError, ValueError) as ex:
            if time.monotonic() > deadline:
                raise FileNotFoundError(f"'{path}' did not arrive within {FRAME_ARRIVAL_TIMEOUT_S:g} s: {ex}") from ex
            time.sleep(FRAME_POLL_INTERVAL_S)


def read_frame(path: Path) -> tuple[fits.Header, np.ndarray]:
    def read(p: Path) -> tuple[fits.Header, np.ndarray]:
        data = np.asarray(fits.getdata(p, memmap=False), dtype=float)
        return fits.getheader(p), data

    return read_arriving(path, read)


def read_json(path: Path) -> dict[str, Any]:
    return read_arriving(path, lambda p: json.loads(p.read_text(encoding="utf-8")))


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
        self.record = RunRecord(
            params=params, folder=str(self.folder), started_at=_iso_now(), camera=self.camera.model_dump()
        )
        self._stage = Stage.PREPARING
        self._rule = RingRule(patience_rings=params.patience_rings, max_rings=params.max_rings)
        self._shared_folder: str | None = None
        self._last_aperture_position: tuple[float, float] | None = None
        self._dec_degrees: float | None = None
        self._pixel_scale: float | None = None

    def run(self) -> RunRecord:
        """The whole run. Always writes `result.json`; re-raises what ended a failed run."""
        try:
            try:
                self.record.terminal = self._run()
            except KeyboardInterrupt:
                self.record.terminal = Terminal.ABORTED
                if self._stage is Stage.PREPARING:
                    self.unit.abort()
            except Exception as ex:
                self.record.terminal = Terminal.FAILED
                self.record.error = f"{type(ex).__name__}: {ex}"
                raise
            finally:
                if self._stage is Stage.SPIRAL:
                    self._end_session()
        finally:
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

        # Before the request: once it is sent, the unit may have opened a session, and closing one
        # that never opened only costs an error in the record.
        self._stage = Stage.SPIRAL
        session = self.unit.spiral_new_path(p.x_step_arcsec, p.y_step_arcsec, p.seconds, p.usable_fraction, p.center)
        self.record.session = session
        if session.get(RA_STEP_COMMANDED) is None:
            raise UnitApiError(
                f"{p.unit}: the unit predates on-sky spiral steps (MAST_unit#136); its RA step would be "
                "cos(dec) short on the sky"
            )
        if session.get("shared_folder") is None:
            raise UnitApiError(f"{p.unit}: the spiral's frames will not reach the share: {session.get('share_error')}")
        self._shared_folder = session["shared_folder"]
        reference = f"{self._shared_folder}/{session['reference_image']}"
        header, _data = read_frame(on_share(self.share_root, reference))
        self._dec_degrees = numeric_card(header, DEC_CARD)
        self._pixel_scale = numeric_card(header, PIXEL_SCALE_CARD)

        terminal = self._walk(reference)
        self._end_session()
        self._measure(reference)
        return terminal

    def _walk(self, reference: str) -> Terminal:
        p = self.params
        # The origin's frame is the reference itself: the spiral has not moved yet.
        moved = SpiralStep(cell=(0, 0), ring=0, image=reference, image_error=None, error=None)
        index = 0
        while True:
            step = self._measure_step(index, moved)
            self.record.steps.append(step)
            terminal = self._rule.update(index, step.flux, step.ring)
            self.record.best_index = self._rule.best_index
            if terminal is not None:
                return terminal
            if step.cell is not None and (radius_arcsec(step.cell, p.x_step_arcsec, p.y_step_arcsec) > p.max_radius_arcsec):
                return Terminal.MAX_RADIUS

            moved = self.unit.spiral_next_step(p.seconds)
            if moved.cell is None:
                # A telemetry hiccup must not end a walk that may already hold its peak; the step
                # is measured and recorded, and completes no ring.
                logger.warning(f"step {index + 1}: no cell from the unit ({moved.error})")
            index += 1

    def _measure_step(self, index: int, moved: SpiralStep) -> Step:
        p, cell = self.params, moved.cell
        exposures = [self._flux_exposure(index, n, cell, moved.ring) for n in range(p.number_of_frames)]
        flux, representative = representative_of([e.measurement.net_counts for e in exposures])
        shown = f"{flux:.0f}" if flux is not None else "unmeasurable"
        logger.info(f"step {index}: cell={cell} ring={moved.ring} flux={shown} (median of {len(exposures)})")
        return Step(
            index=index,
            cell=cell,
            ring=moved.ring,
            offset_arcsec=(cell[0] * p.x_step_arcsec, cell[1] * p.y_step_arcsec) if cell is not None else None,
            cell_error=moved.error,
            imager_frame=moved.image,
            imager_error=moved.image_error,
            flux=flux,
            representative=representative,
            saturated=exposures[representative].saturated,
            exposures=exposures,
        )

    def _flux_exposure(self, index: int, n: int, cell: tuple[int, int] | None, ring: int | None) -> FluxExposure:
        frame = self.meter.expose()
        name = f"flux-{index:05d}-{n:02d}.fits"
        self._write_flux_frame(name, frame, index, cell, ring)
        # Centered on the source it finds; when it finds none, re-measured where the last one was
        # found, so every step is compared through the same window.
        measurement = measure_frame(frame.data, last_position=self._last_aperture_position)
        if (
            measurement.position_source is PositionSource.DETECTED
            and measurement.x is not None
            and measurement.y is not None
        ):
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
        """Close the unit's spiral session, which stops tracking.

        The unit takes a final frame and correlates it, of the last cell, kept for the record
        only. If that fails (a final exposure that errors, a session that never opened) the
        session is still closed on the unit's side, and the run's own result does not depend on
        it, so the error is recorded and the run goes on to its measurement.
        """
        self._stage = Stage.MEASURING
        try:
            self.record.unit_end_path = self.unit.spiral_end_path(self.params.seconds)
        except UnitApiError as ex:
            logger.error(f"closing the spiral session: {ex}")
            self.record.unit_end_path_error = str(ex)

    def _measure(self, reference: str) -> None:
        if self.record.best_index is None:
            self.record.no_result_reason = "no step could be measured, so there is no arg-max"
            return
        best = self.record.steps[self.record.best_index]
        if best.imager_frame is None:
            self.record.no_result_reason = f"the arg-max step {best.index} has no imager frame: {best.imager_error}"
            return

        center_x, center_y, center_source = self._center()
        at_reference = best.imager_frame == reference
        if at_reference:
            dx = dy = magnitude = 0.0
            confidence, at_origin, limit = None, None, None
        else:
            _header, reference_data = read_frame(on_share(self.share_root, reference))
            _header, final = read_frame(on_share(self.share_root, best.imager_frame))
            shift, magnitude, limit = measure_pair(
                reference_data,
                final,
                center_x=center_x,
                center_y=center_y,
                usable_fraction=self.params.usable_fraction,
                expect_no_motion=False,
            )
            dx, dy, confidence, at_origin = shift.dx, shift.dy, shift.confidence, shift.at_origin
        self.record.result = Result(
            dx=dx,
            dy=dy,
            argmax_at_reference=at_reference,
            confidence=confidence,
            at_origin=at_origin,
            low_confidence=confidence is not None and confidence < MIN_CONFIDENCE,
            magnitude_px=magnitude,
            max_reliable_shift_px=limit,
            beyond_limit=limit is not None and magnitude > limit,
            fiber_x=center_x,
            fiber_y=center_y,
            fiber_source=center_source,
            proposed_fiber_x=center_x + dx,
            proposed_fiber_y=center_y + dy,
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

    def _center(self) -> tuple[int, int, str]:
        """The correlation's center, as the unit resolved it for its own session: read from the
        record the session leaves beside its frames, which it writes however it ended."""
        session = read_json(on_share(self.share_root, f"{self._shared_folder}/{UNIT_SESSION_RESULT}"))
        return int(session["center_x"]), int(session["center_y"]), str(session["center_source"])

    def _commanded_offset_px(self, offset_arcsec: tuple[float, float] | None) -> tuple[float, float] | None:
        if offset_arcsec is None or not self._pixel_scale or self._pixel_scale <= 0:
            return None
        return (offset_arcsec[0] / self._pixel_scale, offset_arcsec[1] / self._pixel_scale)

    def _write_flux_frame(
        self, name: str, frame: FluxFrame, index: int, cell: tuple[int, int] | None, ring: int | None
    ) -> None:
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
        if cell is not None:
            header["CELLX"] = (cell[0], "spiral cell, RA steps")
            header["CELLY"] = (cell[1], "spiral cell, Dec steps")
            header["RING"] = ring
        header["CREATOR"] = "flux-orchestrator"
        fits.PrimaryHDU(data=frame.data, header=header).writeto(self.folder / name)

    def _write_record(self) -> None:
        path = self.folder / RESULT_FILE
        path.write_text(json.dumps(asdict(self.record), indent=2, default=str), encoding="utf-8")
        logger.info(f"wrote {path}")
