"""Param's fiber-focus run, off the unit: guide on a star with the folding mirror in, then step the
focuser through a fixed list of positions, taking flux frames at each, and measure nothing.

Ported from MAST_unit `src/flux_metering/fiber_focus.py` (`acquire_and_focus_fiber`, `eli/flux-night`
at `a82abc0`). The unit acquires with no handover, the mirror goes to SPEC, `start_guiding` starts
PHD2, and the run waits for the guide start's settle in `status.latest.guiding` (MAST_unit#384).
After that guiding never steers the run: only the focuser and the flux camera are used. The products
are the frames and `result.json`, rewritten after every position; every analysis is done afterwards.
Every run ends with the focuser sent to `known_as_good_position`, and guiding left running.

Differences from the in-unit run, each forced by the API: the settle is read from the unit's record,
which carries PHD2's error text but not its numeric status, and stamps the settle on the unit's
clock; the frames are written here, not on the unit's RAM disk; the unit cannot be claimed, so
nothing stops a second client using the focuser meanwhile; and an operator's `unit/abort` is seen as
`Aborting` in the unit's activities or tracking turned off, where the in-unit run was told directly.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
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
from flux_orchestrator.run import LOG_FORMAT, flux_frame_header, make_run_folder, unit_root
from flux_orchestrator.unit_client import (
    ABORTING,
    MOVING,
    POLL_INTERVAL_S,
    UNIT_PORT,
    AcquisitionFailedError,
    Target,
    UnitApiError,
    UnitClient,
    UnitInterruptedError,
    focuser_of,
    is_active,
    latest_guiding,
)

logger = logging.getLogger(__name__)

RUN_FOLDER = "FiberFocus"
RESULT_FILE = "result.json"
CREATOR = "flux_orchestrator.fiber_focus"
#: The position index is three digits of the frame names.
MAX_POSITIONS = 999
#: What PHD2 reports as the guider backend in `latest.guiding.backend`; the solving guider never settles.
PHD2_BACKEND = "phd2"
#: The unit's activities that mean another job holds the hardware, as the in-unit run refused them.
BUSY = ("Acquiring", "Guiding", "Autofocusing", "StartingUp", "ShuttingDown", "FluxMetering")
DEGREES_PER_HOUR = 15.0


class Terminal(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"
    ACQUISITION_FAILED = "acquisition_failed"
    GUIDING_FAILED = "guiding_failed"
    FAILED = "failed"


class Phase(StrEnum):
    """Where the run is; decides what Ctrl+C and a failure do to the unit."""

    PREPARING = "preparing"
    #: The unit accepted the acquisition: work in flight that only `unit/abort` stops, on Ctrl+C
    #: and on any failure here, a timeout of this side's wait included.
    ACQUIRING = "acquiring"
    #: The mirror moving to SPEC: aborted on Ctrl+C only; a failed move is the unit's to report.
    MIRROR = "mirror"
    GUIDING = "guiding"
    FOCUSING = "focusing"
    RETURNING = "returning"


class SettleOutcome(StrEnum):
    SETTLED = "settled"
    #: PHD2 reported that settling failed.
    FAILED = "failed"
    #: No settle within `guide_settle_timeout_seconds`.
    TIMEOUT = "timeout"
    #: The guide start's record ended with no settle: the guider refused, or PHD2 went away.
    ENDED = "ended"
    ABORTED = "aborted"


class GuidingError(UnitApiError):
    """The guide start was refused, or did not settle."""


@dataclass(frozen=True)
class FiberFocusParams:
    unit: str
    #: Where to acquire. Both coordinates, or a name the unit resolves.
    target: Target = field(default_factory=Target)
    #: Focuser steps between positions. Signed: a negative step sweeps inward.
    step: int = 0
    #: How many positions, the first included.
    count: int = 1
    #: The first focuser position. None: the unit's `known_as_good_position`, read at the start.
    start: int | None = None
    #: Imager exposure for the acquisition's plate solves.
    acquisition_seconds: float = 5.0
    #: Imager gain for the acquisition. None takes the unit's.
    gain: int | None = None
    flux_seconds: float = 5.0
    flux_gain: int = 0
    flux_black_level: int = 3
    frames_per_position: int = 1
    #: Wait after the focuser has arrived, before the position is read and exposed.
    settle_seconds: float = 2.0
    #: A backstop on PHD2's settle, from `start_guiding` to the settle in `latest.guiding`. If PHD2
    #: has to calibrate first, this may expire before it is done.
    guide_settle_timeout_seconds: float = 120.0

    @property
    def flux_exposure_us(self) -> int:
        return round(self.flux_seconds * US_PER_S)

    def positions(self, start: int) -> list[int]:
        return [start + i * self.step for i in range(self.count)]


def frame_name(index: int, measured: int, frame: int) -> str:
    """`flux-PPP-MMMMM-FF.fits`: position index from 001, MEASURED focuser position, and the frame
    within the position from 00. The in-unit run's names."""
    return f"flux-{index + 1:03d}-{measured:05d}-{frame:02d}.fits"


@dataclass
class FrameRecord:
    name: str
    #: On this host's clock. The exposure lies between the two.
    requested_utc: str
    received_utc: str
    saturated_pixels: int


@dataclass
class PositionRecord:
    index: int
    commanded: int
    #: Read once the focuser had arrived and `settle_seconds` had passed.
    measured: int
    settled_utc: str
    frames: list[FrameRecord] = field(default_factory=list)


@dataclass
class GuideSettle:
    outcome: SettleOutcome | None = None
    backend: str | None = None
    #: PHD2's, for a failed settle, or why the record ended.
    error: str | None = None
    #: On this host's clock.
    guide_commanded_utc: str | None = None
    #: On the unit's clock, as `latest.guiding.settled_at` reports it.
    settled_unit_utc: str | None = None
    waited_seconds: float | None = None


@dataclass
class FiberFocusRecord:
    params: FiberFocusParams
    folder: str
    started_at: str
    camera: dict[str, Any]
    #: The first position actually used, and the list commanded.
    start: int | None = None
    commanded_positions: list[int] = field(default_factory=list)
    focuser_limits: tuple[int | None, int | None] | None = None
    ended_at: str | None = None
    terminal: Terminal | None = None
    error: str | None = None
    guide_settle: GuideSettle | None = None
    positions: list[PositionRecord] = field(default_factory=list)
    #: Where the focuser was sent at the end, and where it was when the run let go of it.
    final_focuser_commanded: int | None = None
    final_focuser_measured: int | None = None
    #: Why the focuser was not sent home, or did not get there.
    return_error: str | None = None
    #: This run aborted the unit (an acquisition or mirror move in flight), and what that answered.
    unit_aborted: bool = False
    unit_abort_error: str | None = None
    #: After the abort, the folding mirror was found between the presets and sent to SKY.
    mirror_parked: bool | None = None
    #: The app's stop that ended the run: one a check raised, or a hard stop.
    stopped_by: str | None = None


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def refusal(params: FiberFocusParams, status: dict[str, Any], start: int) -> str | None:
    """Why a run must not start, or None. Checked before anything moves, as the in-unit run did."""
    busy = [activity for activity in BUSY if is_active(status, activity)]
    if busy:
        return f"the unit is busy ({', '.join(busy)})"
    focuser = focuser_of(status)
    if is_active(focuser, MOVING):
        return "the focuser is moving"
    if params.count < 1:
        return "at least one position"
    if params.count > 1 and params.step == 0:
        return "a step of 0 would expose the same position again and again"
    if params.count > MAX_POSITIONS:
        return f"at most {MAX_POSITIONS} positions: the index is three digits of the frame names"
    if params.frames_per_position < 1:
        return "at least one frame per position"
    lower, upper = focuser.get("lower_limit"), focuser.get("upper_limit")
    # The unit's own comparisons (`Focuser.move_relative`).
    outside = [p for p in params.positions(start) if (lower is not None and p < lower) or (upper and p >= upper)]
    if outside:
        return f"positions {outside[0]}..{outside[-1]} are outside the focuser's range [{lower}, {upper})"
    return None


class FiberFocusRun:
    def __init__(
        self,
        params: FiberFocusParams,
        unit: UnitClient,
        meter: FluxMeterClient,
        output_root: Path | None = None,
        stop: StopToken | None = None,
        on_event: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self.params = params
        self.unit = unit
        self.meter = meter
        # Before the folder: a service that is not there should not leave an empty run behind.
        self.camera = meter.info()
        # The unit's own folder by default, as the in-unit run filed its products.
        self.folder = make_run_folder(output_root or unit_root(params.unit), _now(), kind=RUN_FOLDER)
        self.record = FiberFocusRecord(
            params=params, folder=str(self.folder), started_at=_now().isoformat(), camera=self.camera.model_dump()
        )
        self._stop = stop
        self.report = Reporter(str(self.folder), on_event)
        self._phase = Phase.PREPARING

    def run(self) -> FiberFocusRecord:
        """The whole run. Always sends the focuser home once it has been asked to move anything, and
        always writes `result.json`; re-raises what ended a failed run.

        Only work in flight that nothing else stops is aborted on the unit: an acquisition or a
        mirror move, on Ctrl+C, or an acquisition this side gave up waiting on. Guiding, once
        started, is left running on every way out, as the in-unit run left it; only an operator's
        `unit/abort` stops it."""
        try:
            try:
                try:
                    self.record.terminal = self._run()
                finally:
                    # Before any cleanup below: a stop pressed during it must not cut it short.
                    close(self._stop)
            except KeyboardInterrupt:
                self.record.terminal = Terminal.ABORTED
                if self._phase in (Phase.ACQUIRING, Phase.MIRROR):
                    self._abort_unit()
            except UnitInterruptedError as ex:
                self.record.terminal = Terminal.ABORTED
                self.record.error = str(ex)
            except AcquisitionFailedError as ex:
                self.record.terminal = Terminal.ACQUISITION_FAILED
                self.record.error = str(ex)
            except GuidingError as ex:
                self.record.terminal = Terminal.GUIDING_FAILED
                self.record.error = str(ex)
            except Exception as ex:
                self.record.terminal = Terminal.FAILED
                self.record.error = f"{type(ex).__name__}: {ex}"
                if self._phase is Phase.ACQUIRING:
                    # An acquisition this side stopped waiting on is still running on the unit.
                    self._abort_unit()
                raise
            finally:
                if self._phase is not Phase.PREPARING:
                    self._return_focuser()
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

    def _enter(self, phase: Phase) -> None:
        self._phase = phase
        self.report.enter(phase)

    def _run(self) -> Terminal:
        p = self.params
        status = self.unit.status()
        focuser = focuser_of(status)
        start = p.start if p.start is not None else focuser.get("known_as_good_position")
        if start is None:
            raise UnitApiError(f"{p.unit}: no start position given and the focuser reports no known_as_good_position")
        self.record.start = start
        self.record.commanded_positions = p.positions(start)
        self.record.focuser_limits = (focuser.get("lower_limit"), focuser.get("upper_limit"))
        why = refusal(p, status, start)
        if why is not None:
            raise UnitApiError(f"{p.unit}: {why}")

        self.meter.configure(Settings(exposure_us=p.flux_exposure_us, gain=p.flux_gain, black_level=p.flux_black_level))

        logger.info(f"{p.unit}: acquiring")
        # A refused request leaves nothing in flight, so the phase moves only once it is accepted.
        self.unit.start_acquisition(p.target, seconds=p.acquisition_seconds, gain=p.gain)
        self._enter(Phase.ACQUIRING)
        self.unit.wait_for_acquisition()
        # After the acquisition, which normally leaves the mirror in already.
        self._enter(Phase.MIRROR)
        self.unit.put_mirror_in()

        self._enter(Phase.GUIDING)
        self._start_guiding_and_settle()

        self._enter(Phase.FOCUSING)
        for index, commanded in enumerate(self.record.commanded_positions):
            self.record.positions.append(self._expose_position(index, commanded))
            self._write_record()
        # An abort during the last position's frames must not end the run as completed.
        self._check_not_interrupted("after the last position")
        return Terminal.COMPLETED

    def _check_not_interrupted(self, where: str) -> None:
        if self._interrupted(self.unit.status()):
            raise UnitInterruptedError(f"{self.params.unit}: the unit was aborted {where}")

    def _interrupted(self, status: dict[str, Any]) -> bool:
        """An operator's `unit/abort`: it raises `Aborting` and turns tracking off, which guiding
        keeps on. Checked from the guide start on."""
        mount = status.get("mount") or {}
        return is_active(status, ABORTING) or mount.get("tracking") is False

    def _start_guiding_and_settle(self) -> None:
        p = self.params
        settle = GuideSettle(guide_commanded_utc=_now().isoformat())
        self.record.guide_settle = settle
        commanded = time.monotonic()
        try:
            self.unit.start_guiding()
        except UnitApiError as ex:
            settle.outcome = SettleOutcome.ENDED
            settle.error = str(ex)
            raise GuidingError(f"{p.unit}: start_guiding refused: {ex}") from ex

        while True:
            check(self._stop)
            status = self.unit.status()
            waited = time.monotonic() - commanded
            if self._interrupted(status):
                settle.outcome = SettleOutcome.ABORTED
                raise UnitInterruptedError(f"{p.unit}: the unit was aborted while guiding settled")
            if self._settled(status, settle, waited):
                return
            if waited >= p.guide_settle_timeout_seconds:
                settle.outcome = SettleOutcome.TIMEOUT
                settle.waited_seconds = round(waited, 1)
                unread = (
                    ""
                    if latest_guiding(status) is not None
                    else f"; the record was not read: {status.get('errors') or 'no latest.guiding in status'}"
                )
                raise GuidingError(f"{p.unit}: no settle within {p.guide_settle_timeout_seconds:g} s{unread}")
            time.sleep(POLL_INTERVAL_S)

    def _settled(self, status: dict[str, Any], settle: GuideSettle, waited: float) -> bool:
        """True once the guide start has settled; raises when it will not; False to keep waiting.

        A read without the record is a failed read of the unit's records, or a unit before
        MAST_unit#384; the timeout decides which."""
        p = self.params
        record = latest_guiding(status)
        if record is None:
            return False
        settle.backend = record.get("backend")
        if settle.backend is not None and settle.backend != PHD2_BACKEND:
            settle.outcome = SettleOutcome.ENDED
            raise GuidingError(f"{p.unit}: the guider backend is {settle.backend}, which reports no settle")
        if record.get("settle") is not None:
            settle.settled_unit_utc = record.get("settled_at")
            settle.waited_seconds = round(waited, 1)
            settle.error = record.get("settle_error")
            if record["settle"] == SettleOutcome.SETTLED:
                settle.outcome = SettleOutcome.SETTLED
                logger.info(f"{p.unit}: guiding settled after {waited:.1f} s")
                self.report.emit(EventKind.STEP, f"guiding settled after {waited:.0f} s")
                return True
            settle.outcome = SettleOutcome.FAILED
            raise GuidingError(f"{p.unit}: PHD2 reported that settling failed: {settle.error}")
        if record.get("ended") is not None:
            settle.outcome = SettleOutcome.ENDED
            settle.error = "; ".join(record.get("errors") or []) or None
            raise GuidingError(f"{p.unit}: the guide start ended with no settle: {settle.error}")
        return False

    def _expose_position(self, index: int, commanded: int) -> PositionRecord:
        p = self.params
        check(self._stop)
        self.unit.move_focuser(commanded, interrupted=self._interrupted)
        time.sleep(p.settle_seconds)
        # One read for the position and the pointing, so the header samples one instant.
        status = self.unit.status()
        if self._interrupted(status):
            raise UnitInterruptedError(f"{p.unit}: the unit was aborted at position {index + 1}")
        measured = focuser_of(status).get("position")
        if measured is None:
            raise UnitApiError(f"{p.unit}: the focuser reports no position")
        position = PositionRecord(index=index, commanded=commanded, measured=int(measured), settled_utc=_now().isoformat())
        pointing = status.get("mount") or {}
        for n in range(p.frames_per_position):
            check(self._stop)
            if n > 0:
                # The frames already taken stay recorded; the ones after an abort are never taken.
                self._check_not_interrupted(f"at position {index + 1}, before frame {n}")
            name = frame_name(index, position.measured, n)
            frame = self.meter.expose()
            self._write_flux_frame(name, frame, position, n, pointing)
            position.frames.append(
                FrameRecord(
                    name=name,
                    requested_utc=frame.requested.isoformat(),
                    received_utc=frame.received.isoformat(),
                    saturated_pixels=saturated_pixels(frame.data, self.camera.saturation_level),
                )
            )
            self.report.emit(EventKind.FRAME, name, done=n + 1, total=p.frames_per_position, detail={"position": index + 1})
        self.report.emit(
            EventKind.STEP,
            f"position {index + 1}/{p.count}: commanded {commanded}, measured {measured}",
            done=index + 1,
            total=p.count,
            detail={"commanded": commanded, "measured": position.measured, "frames": len(position.frames)},
        )
        logger.info(
            f"position {index + 1}/{p.count}: commanded {commanded}, measured {measured} ({len(position.frames)} frames)"
        )
        return position

    def _return_focuser(self) -> None:
        """Send the focuser to `known_as_good_position`. Waits out an abort first, whose focuser
        stop would otherwise land after the move and leave it short. Recorded, never raised, so it
        cannot hide what ended the run."""
        self._enter(Phase.RETURNING)
        try:
            if is_active(self.unit.status(), ABORTING):
                self.unit.wait_for_abort_done()
            status = self.unit.focuser_home()
        except UnitApiError as ex:
            logger.error(f"returning the focuser: {ex}")
            self.record.return_error = str(ex)
            return
        focuser = focuser_of(status)
        self.record.final_focuser_commanded = focuser.get("known_as_good_position")
        self.record.final_focuser_measured = focuser.get("position")

    def _abort_unit(self) -> None:
        """Abort the unit, then send the folding mirror to SKY if the abort stopped it between the
        presets: the unit refuses every run until it is at one."""
        self.record.unit_aborted = True
        try:
            self.record.mirror_parked = self.unit.abort_and_park()
        except UnitApiError as ex:
            logger.error(f"aborting the unit: {ex}")
            self.record.unit_abort_error = str(ex)

    def _write_flux_frame(
        self, name: str, frame: FluxFrame, position: PositionRecord, n: int, pointing: dict[str, Any]
    ) -> None:
        p = self.params
        header = flux_frame_header(frame, self.camera, p.unit, p.flux_exposure_us, p.flux_gain, p.flux_black_level)
        cards: list[tuple[str, Any, str]] = [
            ("CREATOR", CREATOR, "what wrote this frame"),
            ("RUNDATE", self.folder.parent.parent.name, "observing night (turns at 12:00 UTC)"),
            ("RUNSEQ", self.folder.name, "fiber focus run sequence"),
            ("POSIDX", position.index + 1, "focuser position index, from 1"),
            ("FRAMEIDX", n, "frame within the position, from 0"),
            ("FOCUSPOS", position.measured, "focuser position, measured after settling"),
            ("FOCUSCMD", position.commanded, "focuser position commanded"),
            ("FOCSETTL", p.settle_seconds, "wait after the focuser arrived, seconds"),
        ]
        ra_hours = pointing.get("ra_j2000_hours")
        if ra_hours is not None:
            cards += [
                ("RA", ra_hours * DEGREES_PER_HOUR, "mount J2000 RA, degrees"),
                ("DEC", pointing.get("dec_j2000_degs"), "mount J2000 Dec, degrees"),
                ("EQUINOX", 2000.0, "equinox of RA/DEC"),
                ("RADESYS", "ICRS", "reference frame of RA/DEC"),
                ("HA", pointing.get("ha_hours"), "hour angle, hours"),
                ("LMST", pointing.get("lmst_hours"), "local mean sidereal time, hours"),
            ]
        target = p.target
        if target.ra_j2000_hours is not None and target.dec_j2000_degs is not None:
            cards.append(
                ("OBJECT", f"{target.ra_j2000_hours:.6f}h {target.dec_j2000_degs:+.6f}d", "requested target (J2000)")
            )
        elif target.object_name is not None:
            cards.append(("OBJECT", target.object_name, "requested target"))
        for keyword, value, comment in cards:
            # Unknown values are left out rather than written empty.
            if value is not None:
                header[keyword] = (value, comment)
        fits.PrimaryHDU(data=frame.data, header=header).writeto(self.folder / name)

    def _write_record(self) -> None:
        path = self.folder / RESULT_FILE
        path.write_text(json.dumps(asdict(self.record), indent=2, default=str), encoding="utf-8")


DEFAULTS = FiberFocusParams(unit="")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flux_orchestrator.fiber_focus",
        description="Guide with the folding mirror in, then step the focuser, saving flux frames at every position.",
    )
    parser.add_argument("--unit", required=True, help="the unit's host name, e.g. mast03")
    parser.add_argument("--unit-port", type=int, default=UNIT_PORT)
    parser.add_argument("--flux-meter", required=True, help="the flux-meter service, e.g. http://mast-ns-spec:8003")

    where = parser.add_argument_group("target (coordinates win over a name)")
    where.add_argument("--ra", type=float, help="RA J2000, decimal hours")
    where.add_argument("--dec", type=float, help="Dec J2000, decimal degrees")
    where.add_argument("--object", help="a name the unit resolves")

    sweep = parser.add_argument_group("sweep")
    sweep.add_argument("--start", type=int, help="first focuser position (default: the unit's known_as_good_position)")
    sweep.add_argument("--step", type=int, required=True, help="focuser steps between positions; negative sweeps inward")
    sweep.add_argument("--count", type=int, required=True, help=f"positions, the first included (1-{MAX_POSITIONS})")
    sweep.add_argument("--frames", type=int, default=DEFAULTS.frames_per_position, help="flux frames per position")
    sweep.add_argument("--settle-seconds", type=float, default=DEFAULTS.settle_seconds)

    run = parser.add_argument_group("run")
    run.add_argument("--acquisition-seconds", type=float, default=DEFAULTS.acquisition_seconds)
    run.add_argument("--gain", type=int, help="imager gain for the acquisition (default: the unit's)")
    run.add_argument("--flux-seconds", type=float, default=DEFAULTS.flux_seconds)
    run.add_argument("--flux-gain", type=int, default=DEFAULTS.flux_gain)
    run.add_argument("--flux-black-level", type=int, default=DEFAULTS.flux_black_level)
    run.add_argument("--guide-settle-timeout", type=float, default=DEFAULTS.guide_settle_timeout_seconds)

    paths = parser.add_argument_group("paths")
    paths.add_argument(
        "--output-root",
        type=Path,
        help="default: the unit's folder on the share, \\\\mast-ns-control\\mast-share\\MAST\\<unit>",
    )

    args = parser.parse_args(argv)
    if (args.ra is None) != (args.dec is None):
        parser.error("--ra and --dec go together")
    if args.ra is None and args.object is None:
        parser.error("a target: --ra and --dec, or --object")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    params = FiberFocusParams(
        unit=args.unit,
        target=Target(ra_j2000_hours=args.ra, dec_j2000_degs=args.dec, object_name=args.object),
        start=args.start,
        step=args.step,
        count=args.count,
        acquisition_seconds=args.acquisition_seconds,
        gain=args.gain,
        flux_seconds=args.flux_seconds,
        flux_gain=args.flux_gain,
        flux_black_level=args.flux_black_level,
        frames_per_position=args.frames,
        settle_seconds=args.settle_seconds,
        guide_settle_timeout_seconds=args.guide_settle_timeout,
    )
    run = FiberFocusRun(
        params, UnitClient(args.unit, port=args.unit_port), FluxMeterClient(args.flux_meter), output_root=args.output_root
    )
    record = run.run()
    logging.info(f"{record.terminal}: {len(record.positions)} of {params.count} positions; see {record.folder}")
    return 0 if record.terminal == Terminal.COMPLETED else 1


if __name__ == "__main__":
    sys.exit(main())
