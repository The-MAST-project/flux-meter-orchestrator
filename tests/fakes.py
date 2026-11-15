"""A unit and a flux-meter service, faked at the HTTP layer, sharing one sky.

The unit walks PWI4's square spiral and writes each step's imager frame to a temporary share,
with the field shifted by the cell's commanded offset. The flux camera sees a spot whose
brightness falls off with distance from `peak_cell`, so a run should find that cell and measure
the commanded offset as its shift.
"""

from __future__ import annotations

import copy
import datetime
import io
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from astropy.io import fits

UNIT = "mast01"
SESSION_FOLDER = f"{UNIT}/2026-10-19/Spirals/0001"
REFERENCE = "reference.fits"
IMAGER_SIZE = 300
DEC_DEGREES = 60.0
PIXEL_SCALE = 0.25
STARS = [(80, 95), (150, 210), (220, 70), (120, 160), (60, 230), (240, 180)]

FLUX_SHAPE = (256, 256)
FLUX_SATURATION = 1023
SPOT_SIGMA_PX = 5.4
SPOT_PEAK_COUNTS = 800.0
FLUX_BACKGROUND = 3.0
#: How fast coupling falls off with distance from the peak cell, in cells.
COUPLING_SIGMA_CELLS = 1.5
#: The unit's clock runs this far ahead of mast-ns-spec's.
UNIT_CLOCK_AHEAD_S = 2.0
#: How far before the request a guide frame PHD2 hands back was taken.
GUIDE_FRAME_AGE_S = 30.0
EXPOSURES_FOLDER = f"{UNIT}/2026-10-29/Exposures"


def spiral_cells() -> Iterator[tuple[int, int]]:
    """PWI4's order: legs of 1, 1, 2, 2, 3, 3 ... going +x, -y, -x, +y. Sessions 0001 and 0004
    (2026-08-13) walked (1,0), (1,-1), (0,-1)."""
    x = y = 0
    directions = [(1, 0), (0, -1), (-1, 0), (0, 1)]
    leg, turn = 1, 0
    while True:
        for _ in range(2):
            dx, dy = directions[turn % 4]
            for _ in range(leg):
                x, y = x + dx, y + dy
                yield x, y
            turn += 1
        leg += 1


def star_field(dx: float, dy: float) -> np.ndarray:
    rng = np.random.default_rng(1)
    data = rng.normal(100.0, 5.0, (IMAGER_SIZE, IMAGER_SIZE)).astype(np.float32)
    yy, xx = np.mgrid[0:IMAGER_SIZE, 0:IMAGER_SIZE]
    for y, x in STARS:
        data += 3000.0 * np.exp(-(((yy - y - dy) ** 2 + (xx - x - dx) ** 2) / (2 * 3.0**2)))
    return data


def envelope(value: Any = None, errors: list[str] | None = None) -> httpx.Response:
    return httpx.Response(200, json={"api_version": "1.0", "value": value, "errors": errors})


@dataclass
class Sky:
    share_root: Path
    x_step_arcsec: float = 0.0
    y_step_arcsec: float = 0.0
    peak_cell: tuple[int, int] = (1, -1)
    cell: tuple[int, int] = (0, 0)
    acquisition_outcome: str = "ended"
    #: A step index whose frame fails, reported through `image_error`.
    failing_step: int | None = None
    #: A step index for which PWI4 reports no spiral offset.
    cellless_step: int | None = None
    #: `spiral_end_path`'s final exposure fails; the session still closes, as the unit's does.
    end_fails: bool = False
    #: The unit's share is demoted: its frames stay on the unit.
    share_unmapped: bool = False
    #: A unit from before MAST_unit#349: no `latest.spiral` in its status.
    predates_latest_spiral: bool = False
    #: A unit from before MAST_unit#136's fix: its RA step is RA coordinate, and it does not say so.
    predates_on_sky_steps: bool = False
    #: A unit whose `latest.spiral` does not report the RA step it commanded.
    predates_latest_ra_step: bool = False
    #: A unit from before MAST_unit#385: it ignores `frames=false` and opens a session that takes frames.
    predates_frameless: bool = False
    #: A unit from before MAST_unit#327: `latest.exposure` carries no `frames`.
    predates_frame_records: bool = False
    #: The index, within every series, of a frame that fails.
    failing_frame: int | None = None
    #: PHD2 is guiding, so every series' first frame is the guide frame it already had.
    guiding: bool = False
    #: Every series fails before its first frame, as when the imager has gone away.
    failing_series: bool = False
    #: `spiral_end_path` is refused.
    end_path_refused: bool = False
    #: `unit/abort` answers and stops nothing, as when the work it waits on never lets go.
    abort_lags: bool = False
    calls: list[str] = field(default_factory=list)
    expose_params: list[dict[str, str]] = field(default_factory=list)
    exposure: dict[str, Any] | None = None
    frameless: bool = False
    tracking_before_session: bool = False
    acquisition: dict[str, Any] | None = None
    spiral: dict[str, Any] | None = None
    at_preset: str | None = "sky"
    session_open: bool = False
    steps: int = 0
    tracking: bool = False
    _cells: Iterator[tuple[int, int]] | None = None
    # ------------------------------------------------- the focuser and guiding --
    focuser_position: int = 20000
    focuser_lower: int | None = 0
    focuser_upper: int | None = 60000
    known_as_good: int | None = 20000
    #: Status reads a move shows `Moving` for; 0 when the focuser never leaves (already there).
    move_reads: int = 1
    #: The focuser never arrives.
    focuser_stuck: bool = False
    #: `goto_known_as_good_position` is refused.
    home_refused: bool = False
    focuser_moves: list[int] = field(default_factory=list)
    _moving_reads: int = 0
    _focuser_target: int | None = None
    #: How the guide start goes: "settled", "failed" (PHD2 reports it), "never" (no settle at all)
    #: or "ends" (the guiding thread refuses after the PUT answered).
    settle: str = "settled"
    #: Status reads before the settle (or the ending) shows.
    settle_reads: int = 1
    #: `start_guiding` refused in the response envelope.
    guiding_refused: bool = False
    guider_backend: str = "phd2"
    guiding_record: dict[str, Any] | None = None
    _settle_wait: int = 0
    #: The unit's own activities, as `status.activities_verbal` lists them.
    activities: list[str] = field(default_factory=list)
    #: An operator's `unit/abort` lands as the focuser is sent to the move with this index (from 0).
    operator_abort_at_move: int | None = None
    #: An operator's `unit/abort` lands while the guide start settles.
    operator_abort_while_settling: bool = False
    #: Status reads `Aborting` stays raised after an abort.
    aborting_reads: int = 1
    _aborting: int = 0
    #: The folding mirror never reaches SPEC.
    stage_stuck: bool = False
    stage_target: str = "spec"
    #: An abort caught the mirror mid-move and stopped it between the presets.
    stage_stopped: bool = False
    #: `start_acquisition_and_guiding` is refused in the envelope.
    acquisition_refused: bool = False
    #: Status reads whose `latest` records failed: no `latest.guiding`, the reason in `errors`.
    unreadable_records_reads: int = 0

    def commanded_px(self, cell: tuple[int, int]) -> tuple[float, float]:
        return cell[0] * self.x_step_arcsec / PIXEL_SCALE, cell[1] * self.y_step_arcsec / PIXEL_SCALE

    @property
    def session_on_share(self) -> Path:
        return self.share_root.joinpath(*SESSION_FOLDER.split("/"))

    def write_frame(self, name: str) -> str:
        """Its path on the share, as the unit reports it."""
        path = self.session_on_share / name
        path.parent.mkdir(parents=True, exist_ok=True)
        header = fits.Header()
        header["DEC"] = DEC_DEGREES
        header["PIXSCALE"] = PIXEL_SCALE
        fits.PrimaryHDU(data=star_field(*self.commanded_px(self.cell)), header=header).writeto(path)
        return path.as_posix()

    # ------------------------------------------------------------------ the unit --

    def unit_handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.removeprefix("/mast/api/v1/unit/")
        self.calls.append(method)
        endpoints = {
            "status": lambda _params: envelope(self._status()),
            "start_acquisition_and_guiding": self._acquire,
            "stage/move_to_preset": self._move_stage,
            "spiral_new_path": self._spiral_new_path,
            "spiral_next_step": self._spiral_next_step,
            "spiral_end_path": self._spiral_end_path,
            "expose": self._expose,
            "abort": self._abort,
            "start_guiding": self._start_guiding,
            "focuser/position": self._focuser_position,
            "focuser/goto_known_as_good_position": self._focuser_home,
            "stop_acquisition_and_guiding": self._stop_guiding,
        }
        if method not in endpoints:
            return httpx.Response(404)
        return endpoints[method](dict(request.url.params))

    def _acquire(self, params: dict[str, str]) -> httpx.Response:
        assert params["handover_automatically_to_guider"] == "false"
        if self.acquisition_refused:
            return envelope(errors=["start_acquisition_and_guiding: already acquiring"])
        self.acquisition = {"state": "running", "errors": [], "started": "t", "ended": None, "succeeded": None}
        return envelope()

    def _move_stage(self, params: dict[str, str]) -> httpx.Response:
        self.at_preset = None
        self.stage_target = params.get("preset", "spec")
        self.stage_stopped = False
        return envelope()

    def _spiral_new_path(self, params: dict[str, str]) -> httpx.Response:
        self.frameless = params.get("frames") == "false" and not self.predates_frameless
        if "frames" not in params:
            assert params["save_intermediate_exposures"] == "true"
        self.x_step_arcsec, self.y_step_arcsec = float(params["x_step_arcsec"]), float(params["y_step_arcsec"])
        self.tracking_before_session = self.tracking
        self.session_open, self.tracking, self.cell = True, True, (0, 0)
        self._cells = spiral_cells()
        value: dict[str, Any] = {}
        if not self.predates_on_sky_steps:
            ra_step = self.x_step_arcsec / math.cos(math.radians(DEC_DEGREES))
            value |= {"dec_for_ra_step_degs": DEC_DEGREES, "ra_step_commanded_arcsec": ra_step}
        shared = None if self.share_unmapped else self.session_on_share.as_posix()
        self.spiral = {
            "started": "t",
            "ended": None,
            "succeeded": None,
            "errors": [],
            "shared_products_path": shared,
            "frames": not self.frameless,
        }
        if not self.predates_latest_ra_step:
            self.spiral |= value
        if self.frameless:
            return envelope({"folder": "D:/MAST/...", "reference_image": None})
        if not self.share_unmapped:
            self.write_frame(REFERENCE)
        return envelope(value | {"folder": "D:/MAST/...", "reference_image": REFERENCE})

    def _exposing(self) -> bool:
        return self.exposure is not None and self.exposure["ended"] is None

    def _spiral_next_step(self, _params: dict[str, str]) -> httpx.Response:
        assert self.session_open and self._cells is not None
        if self._exposing():
            return envelope(errors=["spiral_next_step: an exposure run is in flight"])
        self.steps += 1
        self.cell = next(self._cells)
        value: dict[str, Any] = {"step#": self.steps, "image": None}
        if self.steps == self.cellless_step:
            value["error"] = "PWI4 reported no spiral offset"
        else:
            value |= {"cell": list(self.cell), "ring": max(map(abs, self.cell))}
        if not self.frameless:
            if self.steps == self.failing_step:
                value["image_error"] = "exposure of 'step-0001.fits' failed"
            else:
                value["image"] = self.write_frame(f"step-{self.steps:04d}.fits")
        return envelope(value)

    def _spiral_end_path(self, _params: dict[str, str]) -> httpx.Response:
        if not self.session_open:
            return envelope(errors=["spiral_end_path: no spiral session is open"])
        if self._exposing():
            return envelope(errors=["spiral_end_path: an expose run is in flight"])
        if self.end_path_refused:
            return envelope(errors=["spiral_end_path: refused"])
        if self.frameless:
            self.session_open, self.tracking = False, self.tracking_before_session
            self._end_spiral(succeeded=True)
            return envelope({"steps": self.steps})
        self.session_open, self.tracking = False, False
        # Written on every ending, as the unit's `_result` is, and only where the frames went.
        result = {"center_x": IMAGER_SIZE // 2, "center_y": IMAGER_SIZE // 2, "center_source": "fake"}
        if not self.share_unmapped:
            path = self.session_on_share / "result.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result))
        if self.end_fails:
            return envelope(errors=["spiral_end_path: final exposure failed"])
        return envelope(result)

    def _end_spiral(self, succeeded: bool) -> None:
        if self.spiral is not None:
            self.spiral |= {"ended": self.unit_now().isoformat(), "succeeded": succeeded}

    def unit_now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=UNIT_CLOCK_AHEAD_S)

    def _expose(self, params: dict[str, str]) -> httpx.Response:
        assert self.session_open and self.frameless, "expose only inside a frameless session"
        assert not {"ra_j2000_hours", "dec_j2000_degs", "ra_offsets", "dec_offsets"} & params.keys()
        if self._exposing():
            return envelope(errors=["expose: an exposure run is already in flight"])
        self.expose_params.append(params)
        subfolder = params["subfolder"]
        shared = None if self.share_unmapped else (self.share_root / EXPOSURES_FOLDER / subfolder).as_posix()
        self.exposure = {
            "started": self.unit_now().isoformat(),
            "ended": None,
            "succeeded": None,
            "errors": [],
            "local_products_path": f"D:/MAST/{EXPOSURES_FOLDER}/{subfolder}",
            "shared_products_path": shared,
            "exposure_seconds": float(params["exposure_seconds"]),
            "repeats": int(params["repeats"]),
            "frames": [],
            "completed": 0,
        }
        return envelope()

    def _advance_exposure(self) -> None:
        """One frame per status read; the series ends with its last."""
        exposure = self.exposure
        if exposure is None or exposure["ended"] is not None:
            return
        if self.failing_series:
            failure = "the exposure run failed: PHD2 is not connected"
            exposure |= {"ended": self.unit_now().isoformat(), "succeeded": False, "errors": [failure]}
            return
        n = len(exposure["frames"])
        frame: dict[str, Any] = {"name": f"{n:04d}.fits", "date_obs": None, "exposure_seconds": None, "error": None}
        if n == self.failing_frame:
            frame["error"] = "PHD2 saved no file"
        else:
            # A series runs in milliseconds here, so a frame is stamped no earlier than its request.
            requested = datetime.datetime.fromisoformat(exposure["started"])
            started = max(requested, self.unit_now() - datetime.timedelta(seconds=exposure["exposure_seconds"]))
            if self.guiding and n == 0:
                started = requested - datetime.timedelta(seconds=GUIDE_FRAME_AGE_S)
            frame |= {"date_obs": started.isoformat(), "exposure_seconds": exposure["exposure_seconds"]}
            # As the unit's mover does: the frame lands on the share under the series' folder.
            if exposure["shared_products_path"] is not None:
                target = Path(exposure["shared_products_path"]) / frame["name"]
                target.parent.mkdir(parents=True, exist_ok=True)
                fits.PrimaryHDU(data=np.zeros((4, 4), dtype=np.uint16)).writeto(target, overwrite=True)
        exposure["frames"].append(frame)
        exposure["completed"] = sum(1 for f in exposure["frames"] if f["error"] is None)
        if len(exposure["frames"]) == exposure["repeats"]:
            # As the unit does: a series with a failed frame did not succeed.
            failure = None if exposure["completed"] == exposure["repeats"] else f"{exposure['completed']} of {n + 1} frames"
            exposure |= {
                "ended": self.unit_now().isoformat(),
                "succeeded": failure is None,
                "errors": [failure] if failure else [],
            }

    # ------------------------------------------------------------ guiding and focus --

    def _start_guiding(self, _params: dict[str, str]) -> httpx.Response:
        if "Guiding" in self.activities:
            return envelope(errors=["endpoint_start_guiding: already guiding"])
        if self.guiding_refused:
            return envelope(errors=["endpoint_start_guiding: self.unit.mount is None"])
        self.tracking = True
        self.activities.append("Guiding")
        self.guiding_record = {
            "started": self.unit_now().isoformat(),
            "ended": None,
            "succeeded": None,
            "errors": [],
            "backend": self.guider_backend,
            "settle": None,
            "settled_at": None,
            "settle_error": None,
        }
        self._settle_wait = self.settle_reads
        return envelope()

    def _advance_guiding(self) -> None:
        record = self.guiding_record
        if record is None or record["ended"] is not None or record["settle"] is not None:
            return
        if self.operator_abort_while_settling:
            self.operator_abort()
            return
        if self._settle_wait > 0:
            self._settle_wait -= 1
            return
        match self.settle:
            case "settled":
                record |= {"settle": "settled", "settled_at": self.unit_now().isoformat()}
            case "failed":
                record |= {"settle": "failed", "settled_at": self.unit_now().isoformat(), "settle_error": "Lost star"}
            case "ends":
                record |= {"ended": self.unit_now().isoformat(), "succeeded": False, "errors": ["PHD2: not connected"]}
                self.activities.remove("Guiding")

    def _stop_guiding(self, _params: dict[str, str]) -> httpx.Response:
        if "Guiding" in self.activities:
            self.activities.remove("Guiding")
        if self.guiding_record is not None and self.guiding_record["ended"] is None:
            self.guiding_record |= {"ended": self.unit_now().isoformat(), "succeeded": True}
        return envelope()

    def _focuser_position(self, params: dict[str, str]) -> httpx.Response:
        target = int(params["position"])
        if len(self.focuser_moves) == self.operator_abort_at_move:
            self.operator_abort()
        self.focuser_moves.append(target)
        self._start_move(target)
        return envelope()

    def _focuser_home(self, _params: dict[str, str]) -> httpx.Response:
        if self.home_refused or self.known_as_good is None:
            return envelope(errors=["known_as_good_position is None"])
        self._start_move(self.known_as_good)
        return envelope()

    def _start_move(self, target: int) -> None:
        # As `goto_position`: no `Moving` at all when already there.
        self._focuser_target = target
        self._moving_reads = 0 if target == self.focuser_position else self.move_reads

    def _arrive(self) -> None:
        """A move whose `Moving` reads are spent has arrived, in the same read that shows it ended,
        as the unit lowers `Moving` only on arrival."""
        if self._focuser_target is not None and not self.focuser_stuck and self._moving_reads == 0:
            self.focuser_position, self._focuser_target = self._focuser_target, None

    def operator_abort(self) -> None:
        """What `Unit.abort` does to guiding and the mount: guiding stopped (the record ends,
        succeeded), tracking off, the focuser stopped where it is, and `Aborting` raised for a
        while."""
        if "Guiding" in self.activities:
            self.activities.remove("Guiding")
        if self.guiding_record is not None and self.guiding_record["ended"] is None:
            self.guiding_record |= {"ended": self.unit_now().isoformat(), "succeeded": True}
        self.tracking = False
        # A stuck focuser stands for the unit's timer re-sending a move that stalled short.
        if not self.focuser_stuck:
            self._focuser_target = None
        self._aborting = self.aborting_reads

    def _abort(self, _params: dict[str, str]) -> httpx.Response:
        if self.abort_lags:
            return envelope()
        if self.at_preset is None:
            # As Stage.abort does: the mirror stops where it is.
            self.stage_stopped = True
        if self.exposure is not None and self.exposure["ended"] is None:
            self.exposure |= {"ended": self.unit_now().isoformat(), "succeeded": False}
        if self.session_open:
            self.session_open = False
            self.tracking = self.tracking_before_session if self.frameless else False
            self._end_spiral(succeeded=False)
        return envelope()

    def _status(self) -> dict[str, Any]:
        latest: dict[str, Any] = {"acquisition": self.acquisition}
        if self.exposure is not None:
            shown = copy.deepcopy(self.exposure)
            if self.predates_frame_records:
                shown.pop("frames")
            latest["exposure"] = shown
        self._advance_exposure()
        if not self.predates_latest_spiral:
            latest["spiral"] = self.spiral
        unreadable = self.unreadable_records_reads > 0
        if self.guiding_record is not None and not unreadable:
            latest["guiding"] = dict(self.guiding_record)
        self._arrive()
        moving = self._focuser_target is not None
        activities = [*self.activities, *(["Aborting"] if self._aborting > 0 else [])]
        status = {
            "latest": latest,
            "stage": {"at_preset": self.at_preset},
            "activities_verbal": activities or None,
            "errors": ["latest: PWI4 did not answer"] if unreadable else [],
            "mount": {
                "tracking": self.tracking,
                "ra_j2000_hours": 1.0,
                "dec_j2000_degs": 60.0,
                "ha_hours": -0.5,
                "lmst_hours": 0.5,
            },
            "focuser": {
                "activities_verbal": ["Moving"] if moving else None,
                "position": self.focuser_position,
                "lower_limit": self.focuser_lower,
                "upper_limit": self.focuser_upper,
                "known_as_good_position": self.known_as_good,
            },
        }
        if self._aborting > 0:
            self._aborting -= 1
        if unreadable:
            self.unreadable_records_reads -= 1
        self._advance_guiding()
        if self._moving_reads > 0:
            self._moving_reads -= 1
        # Each read moves the slow things one poll along.
        if self.acquisition is not None and self.acquisition["state"] == "running":
            self.acquisition = {**self.acquisition, "state": self.acquisition_outcome}
        if self.at_preset is None and not self.stage_stuck and not self.stage_stopped:
            self.at_preset = self.stage_target
        return status

    # ------------------------------------------------------- the flux-meter service --

    def flux_handler(self, request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/info":
                info = {"model": "FAKE", "serial_number": "0", "saturation_level": FLUX_SATURATION, "description": "fake"}
                return httpx.Response(200, json=info)
            case "/configure":
                assert json.loads(request.content)["exposure_us"] > 0
                return httpx.Response(200, json=None)
            case "/expose":
                buffer = io.BytesIO()
                np.save(buffer, self._flux_frame(), allow_pickle=False)
                return httpx.Response(200, content=buffer.getvalue())
        return httpx.Response(404)

    def _flux_frame(self) -> np.ndarray:
        d2 = (self.cell[0] - self.peak_cell[0]) ** 2 + (self.cell[1] - self.peak_cell[1]) ** 2
        coupling = math.exp(-d2 / (2.0 * COUPLING_SIGMA_CELLS**2))
        ny, nx = FLUX_SHAPE
        yy, xx = np.mgrid[0:ny, 0:nx]
        spot = np.exp(-((xx - nx / 2) ** 2 + (yy - ny / 2) ** 2) / (2.0 * SPOT_SIGMA_PX**2))
        return np.clip(FLUX_BACKGROUND + SPOT_PEAK_COUNTS * coupling * spot, 0, FLUX_SATURATION).astype(np.uint16)
