"""Param's save-spirals run, off the unit: a fixed number of spiral rings, imager and flux frames
at every cell, and nothing measured.

Ported from MAST_unit `src/flux_metering/spirals.py` (`acquire_and_save_spirals`, `eli/flux-night`
at `a82abc0`). The unit acquires, puts the folding mirror in and opens a spiral session that takes
no frames (MAST_unit#385). At each cell `expose` takes `number_of_frames` imager frames into a
subfolder of its own, while this takes flux frames back to back from the moment `expose` answers
until the unit's record of that series ends. Each imager frame is then paired with the flux frame
that overlaps it most, across the two hosts' clocks. The products are the frames and `result.json`,
rewritten after every cell; every analysis is done afterwards.

Differences from the in-unit run, each forced by the API: the unit names the imager frames, so the
flux frames name their cell and the imager subfolder instead of a partner frame; a frame's pairing
is in `result.json` rather than in its header; and the imager's gain defaults to `expose`'s.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from astropy.io import fits

from flux_meter_service.models import Settings
from flux_orchestrator.flux_client import US_PER_S, FluxFrame, FluxMeterClient
from flux_orchestrator.photometry import saturated_pixels
from flux_orchestrator.progress import EventKind, Reporter, RunEvent, StopToken, check, close, stopped_by
from flux_orchestrator.run import DEFAULT_OUTPUT_ROOT, LOG_FORMAT, RA_STEP_COMMANDED, flux_frame_header, make_run_folder
from flux_orchestrator.unit_client import UNIT_PORT, SpiralStep, Target, UnitApiError, UnitClient

logger = logging.getLogger(__name__)

#: The in-unit run's folder name, which Param's analysis reads (Param, 2026-11-15).
RUN_FOLDER = "FluxMetering"
RESULT_FILE = "result.json"
#: The ring is one digit of the flux frames' names.
MAX_RINGS = 9
#: Beyond the exposures themselves: PHD2's save of a full frame, and the move off the RAM disk
#: that `expose` waits for between frames.
FRAME_OVERHEAD_S = 60.0
#: For `unit/abort`, which answers before it has stopped anything, to end the session and the
#: series: the imager's abort releasing a frame in flight, and the session lock letting go.
ABORT_CONFIRM_S = 30.0


class SpiralsTerminal(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass(frozen=True)
class SpiralsParams:
    unit: str
    #: Imager exposure for every frame `expose` takes at a cell.
    step_seconds: float = 5.0
    #: Imager exposure for the acquisition's plate solves.
    acquisition_seconds: float = 5.0
    flux_seconds: float = 5.0
    target: Target = field(default_factory=Target)
    #: The imager's. None: `expose`'s default for the steps, the unit's for the acquisition.
    gain: int | None = None
    #: Arcsec on the sky; the unit sizes the RA step for the declination (MAST_unit#136).
    x_step_arcsec: float = 0.5
    y_step_arcsec: float = 0.5
    #: Rings walked around the origin cell; the run takes `total_cells` steps and stops.
    rings: int = 2
    flux_gain: int = 0
    flux_black_level: int = 3
    #: Imager frames per cell.
    number_of_frames: int = 1
    #: Engineering only: walk wherever the mount points. Echoed into the result.
    skip_acquisition: bool = False

    @property
    def flux_exposure_us(self) -> int:
        return round(self.flux_seconds * US_PER_S)

    @property
    def total_cells(self) -> int:
        """The origin, plus 8n cells on ring n."""
        return (2 * self.rings + 1) ** 2


def ring_position(index: int) -> tuple[int, int]:
    """(ring, step within the ring) of step `index` of a square spiral, worked out from the counter
    rather than read from PWI4, so a name is defined even when PWI4 reports no offset. The ring
    counts from 0 and the step within it from 1; ring n starts at step (2n - 1)**2."""
    ring = (math.isqrt(index) + 1) // 2
    first = (2 * ring - 1) ** 2 if ring else 0
    return ring, index - first + 1


@dataclass(frozen=True)
class Window:
    start: datetime.datetime
    end: datetime.datetime

    def overlap_seconds(self, other: Window) -> float:
        return max(0.0, (min(self.end, other.end) - max(self.start, other.start)).total_seconds())


@dataclass(frozen=True)
class ClockOffset:
    """The unit's clock minus this host's.

    The unit stamps its exposure record's `started` between receiving `expose` and answering it,
    so that instant lies between this host's send and receipt: the offset is bracketed by the
    round trip, measured at every cell rather than assumed."""

    seconds: float
    #: Half the bracket.
    uncertainty_seconds: float

    @classmethod
    def bracketed(cls, unit_time: datetime.datetime, sent: datetime.datetime, answered: datetime.datetime) -> ClockOffset:
        low, high = (unit_time - answered).total_seconds(), (unit_time - sent).total_seconds()
        return cls(seconds=(low + high) / 2, uncertainty_seconds=(high - low) / 2)

    def to_local(self, unit_time: datetime.datetime) -> datetime.datetime:
        return unit_time - datetime.timedelta(seconds=self.seconds)


def best_overlap(window: Window, others: Sequence[Window]) -> tuple[int | None, float]:
    """The index of the window in `others` overlapping `window` most, and by how much; None when
    none overlaps it."""
    overlaps = [window.overlap_seconds(other) for other in others]
    if not overlaps or max(overlaps) <= 0.0:
        return None, 0.0
    best = max(range(len(overlaps)), key=overlaps.__getitem__)
    return best, overlaps[best]


@dataclass
class FluxFrameRecord:
    name: str
    #: On this host's clock. The exposure lies between the two; the window used for pairing is
    #: this whole interval, transfer included.
    requested_utc: str
    received_utc: str
    saturated_pixels: int


@dataclass
class Pair:
    #: Under the cell's `shared_folder`.
    imager_frame: str
    imager_error: str | None
    #: The frame's `DATE-OBS`, in the unit's clock, as the unit reports it.
    date_obs: str | None
    exposure_seconds: float | None
    #: The imager frame's window moved onto this host's clock; None without `date_obs`.
    imager_start_local_utc: str | None
    imager_end_local_utc: str | None
    #: The frame started before `expose` was asked for it: PHD2, guiding, saved the guide frame
    #: it already had, and `exposure_seconds` is then the guide exposure.
    predates_request: bool
    flux_frame: str | None
    overlap_seconds: float


@dataclass
class Cell:
    index: int
    #: None when PWI4 reported no spiral offset for the step (`cell_error` says so).
    cell: tuple[int, int] | None
    ring: int | None
    offset_arcsec: tuple[float, float] | None
    cell_error: str | None
    subfolder: str
    expose_sent_utc: str
    expose_answered_utc: str
    clock_offset: ClockOffset | None = None
    #: `status.latest.exposure` as the series ended.
    exposure: dict[str, Any] | None = None
    shared_folder: str | None = None
    flux_frames: list[FluxFrameRecord] = field(default_factory=list)
    pairs: list[Pair] = field(default_factory=list)


@dataclass
class SpiralsRecord:
    params: SpiralsParams
    folder: str
    started_at: str
    camera: dict[str, Any]
    ended_at: str | None = None
    terminal: SpiralsTerminal | None = None
    error: str | None = None
    session: dict[str, Any] | None = None
    #: `status.latest.spiral` as the session opened.
    spiral: dict[str, Any] | None = None
    unit_end_path: dict[str, Any] | None = None
    #: Why the unit was aborted rather than its session closed, and what that answered.
    unit_aborted: bool = False
    unit_abort_error: str | None = None
    #: The unit's records said the session and the series had ended; None when the abort failed.
    unit_abort_confirmed: bool | None = None
    #: The app's stop that ended the run: one a check raised, or a hard stop.
    stopped_by: str | None = None
    cells: list[Cell] = field(default_factory=list)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _parse_utc(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


class SpiralsRun:
    def __init__(
        self,
        params: SpiralsParams,
        unit: UnitClient,
        meter: FluxMeterClient,
        output_root: Path = DEFAULT_OUTPUT_ROOT,
        stop: StopToken | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self.params = params
        self.unit = unit
        self.meter = meter
        # Before the folder: a service that is not there should not leave an empty run behind.
        self.camera = meter.info()
        self.folder = make_run_folder(output_root, _now(), kind=RUN_FOLDER)
        self.record = SpiralsRecord(
            params=params, folder=str(self.folder), started_at=_now().isoformat(), camera=self.camera.model_dump()
        )
        #: Ties each cell's imager subfolder on the unit to this run's folder here.
        self._subfolder_prefix = f"{RUN_FOLDER}-{self.folder.parent.parent.name}-{self.folder.name}"
        #: The unit has been asked to do something for this run and not yet released.
        self._engaged = False
        self._stop = stop
        self.report = Reporter(str(self.folder), on_event)

    def run(self) -> SpiralsRecord:
        """The whole run. Always writes `result.json`; re-raises what ended a failed run.

        A finished walk closes the session, which leaves tracking as it was found. An interrupted
        or failed one aborts the unit instead: an `expose` series may be in flight, which
        `spiral_end_path` refuses, and `unit/abort` ends both."""
        try:
            try:
                try:
                    self.record.terminal = self._run()
                finally:
                    # Before any cleanup below: a stop pressed during it must not cut it short.
                    close(self._stop)
            except KeyboardInterrupt:
                self.record.terminal = SpiralsTerminal.ABORTED
                self._abort_unit()
            except Exception as ex:
                self.record.terminal = SpiralsTerminal.FAILED
                self.record.error = f"{type(ex).__name__}: {ex}"
                self._abort_unit()
                raise
        finally:
            self.record.stopped_by = stopped_by(self._stop)
            self.record.ended_at = _now().isoformat()
            self._write_record()
            self.report.emit(
                EventKind.END,
                str(self.record.terminal),
                detail={"terminal": self.record.terminal, "error": self.record.error},
            )
        return self.record

    def _run(self) -> SpiralsTerminal:
        p = self.params
        self.meter.configure(Settings(exposure_us=p.flux_exposure_us, gain=p.flux_gain, black_level=p.flux_black_level))

        self._engaged = True
        if p.skip_acquisition:
            logger.warning("acquisition SKIPPED: this run is a shakedown, not centered on the fiber")
        else:
            logger.info(f"{p.unit}: acquiring")
            self.report.enter("acquiring")
            self.unit.acquire(p.target, seconds=p.acquisition_seconds, gain=p.gain)
        # After the acquisition: it takes the mirror out to solve and puts it back.
        self.report.enter("mirror")
        self.unit.put_mirror_in()

        self.record.session = self.unit.spiral_new_frameless_path(p.x_step_arcsec, p.y_step_arcsec)
        self.record.spiral = self.unit.latest_spiral()
        if self.record.spiral.get("frames") is not False:
            raise UnitApiError(
                f"{p.unit}: the unit predates frameless spiral sessions (MAST_unit#385); it opened one that takes frames"
            )
        if self.record.spiral.get(RA_STEP_COMMANDED) is None:
            raise UnitApiError(
                f"{p.unit}: status.latest.spiral reports no commanded RA step, so the unit's spiral steps cannot "
                "be confirmed square on the sky (MAST_unit#136)"
            )

        self.report.enter("walking")
        terminal = self._walk()
        self.report.enter("closing")
        self._end_session()
        return terminal

    def _walk(self) -> SpiralsTerminal:
        """Walk `rings` rings and stop. PWI4 reporting a cell beyond the last ring ends the walk;
        the cell count is the backstop for a PWI4 that reports no offset, checked before moving
        so the mount never steps onto a ring that will not be exposed."""
        p = self.params
        moved = SpiralStep(cell=(0, 0), ring=0, image=None, image_error=None, error=None)
        index = 0
        while True:
            if moved.ring is not None and moved.ring > p.rings:
                return SpiralsTerminal.COMPLETED
            cell = self._expose_cell(index, moved)
            self.record.cells.append(cell)
            self._write_record()
            self.report.emit(
                EventKind.STEP,
                f"cell {index + 1}/{p.total_cells}: {cell.cell} ring {cell.ring}, {len(cell.flux_frames)} flux frames",
                done=index + 1,
                total=p.total_cells,
                detail={
                    "cell": cell.cell,
                    "ring": cell.ring,
                    "flux_frames": len(cell.flux_frames),
                    "imager": cell.subfolder,
                },
            )
            if index + 1 >= p.total_cells:
                return SpiralsTerminal.COMPLETED
            check(self._stop)
            moved = self.unit.spiral_next_step()
            if moved.cell is None:
                logger.warning(f"step {index + 1}: no cell from the unit ({moved.error})")
            index += 1

    def _expose_cell(self, index: int, moved: SpiralStep) -> Cell:
        p = self.params
        ring, within = ring_position(index)
        subfolder = f"{self._subfolder_prefix}-{ring}-{within:03d}"
        sent = _now()
        self.unit.expose(subfolder, p.step_seconds, p.number_of_frames, p.gain)
        answered = _now()
        cell = Cell(
            index=index,
            cell=moved.cell,
            ring=moved.ring,
            offset_arcsec=(moved.cell[0] * p.x_step_arcsec, moved.cell[1] * p.y_step_arcsec) if moved.cell else None,
            cell_error=moved.error,
            subfolder=subfolder,
            expose_sent_utc=sent.isoformat(),
            expose_answered_utc=answered.isoformat(),
        )

        flux: list[FluxFrame] = []
        deadline = time.monotonic() + p.number_of_frames * (p.step_seconds + FRAME_OVERHEAD_S)
        while True:
            check(self._stop)
            name = f"flux-{ring}-{within:03d}-{len(flux):03d}.fits"
            frame = self.meter.expose()
            self._write_flux_frame(name, frame, cell)
            flux.append(frame)
            cell.flux_frames.append(
                FluxFrameRecord(
                    name=name,
                    requested_utc=frame.requested.isoformat(),
                    received_utc=frame.received.isoformat(),
                    saturated_pixels=saturated_pixels(frame.data, self.camera.saturation_level),
                )
            )
            exposure = self._own_exposure(subfolder)
            if exposure["shared_products_path"] is None:
                raise UnitApiError(
                    f"{p.unit}: the frames in '{subfolder}' will not reach the share: the unit's share is demoted"
                )
            if exposure["ended"] is not None:
                break
            if time.monotonic() > deadline:
                raise UnitApiError(f"{p.unit}: the exposure series in '{subfolder}' did not end in time")

        if exposure["completed"] == 0:
            raise UnitApiError(f"{p.unit}: the series in '{subfolder}' saved no frames: {'; '.join(exposure['errors'])}")
        cell.exposure = exposure
        cell.shared_folder = exposure["shared_products_path"]
        cell.clock_offset = ClockOffset.bracketed(_parse_utc(exposure["started"]), sent, answered)
        cell.pairs = self._pairs(exposure, cell.clock_offset, flux, cell.flux_frames)
        logger.info(
            f"step {index}: cell={moved.cell} ring={moved.ring}: {exposure['completed']} of {p.number_of_frames} "
            f"imager frames, {len(flux)} flux frames"
        )
        return cell

    def _own_exposure(self, subfolder: str) -> dict[str, Any]:
        """The unit's record of the series this cell started, refused if it is another's or the
        unit does not report frames."""
        exposure = self.unit.latest_exposure()
        local = exposure.get("local_products_path") or ""
        if not local.replace("\\", "/").endswith(f"/{subfolder}"):
            raise UnitApiError(f"{self.params.unit}: the latest exposure is not this cell's ('{local}')")
        if "frames" not in exposure:
            raise UnitApiError(f"{self.params.unit}: the unit predates per-frame exposure records (MAST_unit#327)")
        return exposure

    @staticmethod
    def _pairs(
        exposure: dict[str, Any], offset: ClockOffset, flux: list[FluxFrame], records: list[FluxFrameRecord]
    ) -> list[Pair]:
        requested = _parse_utc(exposure["started"])
        flux_windows = [Window(frame.requested, frame.received) for frame in flux]
        pairs = []
        for frame in exposure["frames"]:
            date_obs, seconds = frame.get("date_obs"), frame.get("exposure_seconds")
            if date_obs is None or seconds is None:
                pairs.append(
                    Pair(
                        imager_frame=frame["name"],
                        imager_error=frame.get("error"),
                        date_obs=date_obs,
                        exposure_seconds=seconds,
                        imager_start_local_utc=None,
                        imager_end_local_utc=None,
                        predates_request=False,
                        flux_frame=None,
                        overlap_seconds=0.0,
                    )
                )
                continue
            start = offset.to_local(_parse_utc(date_obs))
            window = Window(start, start + datetime.timedelta(seconds=seconds))
            best, overlap = best_overlap(window, flux_windows)
            pairs.append(
                Pair(
                    imager_frame=frame["name"],
                    imager_error=frame.get("error"),
                    date_obs=date_obs,
                    exposure_seconds=seconds,
                    imager_start_local_utc=window.start.isoformat(),
                    imager_end_local_utc=window.end.isoformat(),
                    predates_request=_parse_utc(date_obs) < requested,
                    flux_frame=records[best].name if best is not None else None,
                    overlap_seconds=overlap,
                )
            )
        return pairs

    def _end_session(self) -> None:
        """Close the session; a frameless one leaves tracking as it was found. The unit stays
        engaged until it has, so a refusal or an interruption aborts it instead."""
        self.record.unit_end_path = self.unit.spiral_end_path()
        self._engaged = False

    def _abort_unit(self) -> None:
        """Abort what the unit is doing for this run, which also closes the session (MAST_unit#385).
        Recorded rather than raised, so it cannot hide what ended the run."""
        if not self._engaged:
            return
        self._engaged = False
        self.record.unit_aborted = True
        try:
            self.unit.abort()
        except UnitApiError as ex:
            logger.error(f"aborting the unit: {ex}")
            self.record.unit_abort_error = str(ex)
            return
        try:
            self.unit.wait_for_released(ABORT_CONFIRM_S)
        except UnitApiError as ex:
            logger.error(f"after the abort: {ex}")
            self.record.unit_abort_confirmed = False
        else:
            self.record.unit_abort_confirmed = True

    def _write_flux_frame(self, name: str, frame: FluxFrame, cell: Cell) -> None:
        p = self.params
        ring, within = ring_position(cell.index)
        header = flux_frame_header(frame, self.camera, p.unit, p.flux_exposure_us, p.flux_gain, p.flux_black_level)
        header["STEPIDX"] = (cell.index, "spiral step index")
        header["NAMERING"] = (ring, "ring in the frame name")
        header["RINGSTEP"] = (within, "step within that ring, from 1")
        if cell.cell is not None:
            header["CELLX"] = (cell.cell[0], "spiral cell, RA steps")
            header["CELLY"] = (cell.cell[1], "spiral cell, Dec steps")
            header["RING"] = (cell.ring, "spiral ring, as PWI4 reports it")
        if cell.offset_arcsec is not None:
            header["OFFRA"] = (cell.offset_arcsec[0], "RA offset on the sky, arcsec")
            header["OFFDEC"] = (cell.offset_arcsec[1], "Dec offset, arcsec")
        header["IMGDIR"] = (cell.subfolder, "unit expose subfolder")
        header["IMGEXPT"] = (p.step_seconds, "imager exposure, seconds")
        fits.PrimaryHDU(data=frame.data, header=header).writeto(self.folder / name)

    def _write_record(self) -> None:
        path = self.folder / RESULT_FILE
        path.write_text(json.dumps(asdict(self.record), indent=2, default=str), encoding="utf-8")


DEFAULTS = SpiralsParams(unit="")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flux_orchestrator.save_spirals",
        description="Walk a fixed number of spiral rings, saving imager and flux frames at every cell.",
    )
    parser.add_argument("--unit", required=True, help="the unit's host name, e.g. mast01")
    parser.add_argument("--unit-port", type=int, default=UNIT_PORT)
    parser.add_argument("--flux-meter", required=True, help="the flux-meter service, e.g. http://localhost:8003")

    where = parser.add_argument_group("target (coordinates win over a name; neither: where the telescope points)")
    where.add_argument("--ra", type=float, help="RA J2000, decimal hours")
    where.add_argument("--dec", type=float, help="Dec J2000, decimal degrees")
    where.add_argument("--object", help="a name the unit resolves")
    where.add_argument("--skip-acquisition", action="store_true", help="shakedown only: walk wherever the mount points")

    run = parser.add_argument_group("run")
    run.add_argument("--rings", type=int, choices=range(MAX_RINGS + 1), default=DEFAULTS.rings, metavar="0-9")
    run.add_argument("--step-seconds", type=float, default=DEFAULTS.step_seconds, help="imager exposure per frame")
    run.add_argument("--acquisition-seconds", type=float, default=DEFAULTS.acquisition_seconds)
    run.add_argument("--flux-seconds", type=float, default=DEFAULTS.flux_seconds)
    run.add_argument("--frames", type=int, default=DEFAULTS.number_of_frames, help="imager frames per cell")
    run.add_argument("--gain", type=int, help="imager gain (default: expose's, and the unit's for the acquisition)")
    run.add_argument("--x-step", type=float, default=DEFAULTS.x_step_arcsec, help="RA step, arcsec on the sky")
    run.add_argument("--y-step", type=float, default=DEFAULTS.y_step_arcsec, help="Dec step, arcsec on the sky")
    run.add_argument("--flux-gain", type=int, default=DEFAULTS.flux_gain)
    run.add_argument("--flux-black-level", type=int, default=DEFAULTS.flux_black_level)

    paths = parser.add_argument_group("paths")
    paths.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help=f"default {DEFAULT_OUTPUT_ROOT}")

    args = parser.parse_args(argv)
    if (args.ra is None) != (args.dec is None):
        parser.error("--ra and --dec go together")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    params = SpiralsParams(
        unit=args.unit,
        step_seconds=args.step_seconds,
        acquisition_seconds=args.acquisition_seconds,
        flux_seconds=args.flux_seconds,
        target=Target(ra_j2000_hours=args.ra, dec_j2000_degs=args.dec, object_name=args.object),
        gain=args.gain,
        x_step_arcsec=args.x_step,
        y_step_arcsec=args.y_step,
        rings=args.rings,
        flux_gain=args.flux_gain,
        flux_black_level=args.flux_black_level,
        number_of_frames=args.frames,
        skip_acquisition=args.skip_acquisition,
    )
    run = SpiralsRun(
        params, UnitClient(args.unit, port=args.unit_port), FluxMeterClient(args.flux_meter), output_root=args.output_root
    )
    record = run.run()
    logging.info(f"{record.terminal}: {len(record.cells)} of {params.total_cells} cells; see {record.folder}")
    return 0 if record.terminal == SpiralsTerminal.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
