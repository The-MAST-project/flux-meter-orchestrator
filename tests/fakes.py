"""A unit and a flux-meter service, faked at the HTTP layer, sharing one sky.

The unit walks PWI4's square spiral and writes each step's imager frame to a temporary share,
with the field shifted by the cell's commanded offset. The flux camera sees a spot whose
brightness falls off with distance from `peak_cell`, so a run should find that cell and measure
the commanded offset as its shift.
"""

from __future__ import annotations

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
    #: The unit's frames will not reach the share (Z: unmapped).
    share_unmapped: bool = False
    #: A unit from before MAST_unit#136's fix: its RA step is RA coordinate, and it does not say so.
    predates_on_sky_steps: bool = False
    calls: list[str] = field(default_factory=list)
    acquisition: dict[str, Any] | None = None
    at_preset: str | None = "sky"
    session_open: bool = False
    steps: int = 0
    tracking: bool = False
    _cells: Iterator[tuple[int, int]] | None = None

    def commanded_px(self, cell: tuple[int, int]) -> tuple[float, float]:
        return cell[0] * self.x_step_arcsec / PIXEL_SCALE, cell[1] * self.y_step_arcsec / PIXEL_SCALE

    def write_frame(self, name: str) -> str:
        relative = f"{SESSION_FOLDER}/{name}"
        path = self.share_root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        header = fits.Header()
        header["DEC"] = DEC_DEGREES
        header["PIXSCALE"] = PIXEL_SCALE
        fits.PrimaryHDU(data=star_field(*self.commanded_px(self.cell)), header=header).writeto(path)
        return relative

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
            "abort": lambda _params: envelope(),
        }
        if method not in endpoints:
            return httpx.Response(404)
        return endpoints[method](dict(request.url.params))

    def _acquire(self, params: dict[str, str]) -> httpx.Response:
        assert params["handover_automatically_to_guider"] == "false"
        self.acquisition = {"state": "running", "errors": [], "started": "t", "ended": None, "succeeded": None}
        return envelope()

    def _move_stage(self, _params: dict[str, str]) -> httpx.Response:
        self.at_preset = None
        return envelope()

    def _spiral_new_path(self, params: dict[str, str]) -> httpx.Response:
        assert params["save_intermediate_exposures"] == "true"
        self.x_step_arcsec, self.y_step_arcsec = float(params["x_step_arcsec"]), float(params["y_step_arcsec"])
        self.session_open, self.tracking, self.cell = True, True, (0, 0)
        self._cells = spiral_cells()
        value: dict[str, Any] = {}
        if not self.predates_on_sky_steps:
            ra_step = self.x_step_arcsec / math.cos(math.radians(DEC_DEGREES))
            value |= {"dec_for_ra_step_degs": DEC_DEGREES, "ra_step_commanded_arcsec": ra_step}
        if self.share_unmapped:
            return envelope(value | {"folder": "C:/MAST/...", "shared_folder": None, "share_error": "not on the share"})
        self.write_frame(REFERENCE)
        value |= {"folder": "D:/MAST/...", "shared_folder": SESSION_FOLDER, "reference_image": REFERENCE}
        return envelope(value)

    def _spiral_next_step(self, _params: dict[str, str]) -> httpx.Response:
        assert self.session_open and self._cells is not None
        self.steps += 1
        self.cell = next(self._cells)
        value: dict[str, Any] = {"step#": self.steps, "image": None}
        if self.steps == self.cellless_step:
            value["error"] = "PWI4 reported no spiral offset"
        else:
            value |= {"cell": list(self.cell), "ring": max(map(abs, self.cell))}
        if self.steps == self.failing_step:
            value["image_error"] = "exposure of 'step-0001.fits' failed"
        else:
            value["image"] = self.write_frame(f"step-{self.steps:04d}.fits")
        return envelope(value)

    def _spiral_end_path(self, _params: dict[str, str]) -> httpx.Response:
        if not self.session_open:
            return envelope(errors=["spiral_end_path: no spiral session is open"])
        self.session_open, self.tracking = False, False
        # Written on every ending, as the unit's `_result` is, and only where the frames went.
        result = {"center_x": IMAGER_SIZE // 2, "center_y": IMAGER_SIZE // 2, "center_source": "fake"}
        if not self.share_unmapped:
            path = self.share_root.joinpath(*SESSION_FOLDER.split("/"), "result.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result))
        if self.end_fails:
            return envelope(errors=["spiral_end_path: final exposure failed"])
        return envelope(result)

    def _status(self) -> dict[str, Any]:
        status = {"latest": {"acquisition": self.acquisition}, "stage": {"at_preset": self.at_preset}}
        # Each read moves the slow things one poll along.
        if self.acquisition is not None and self.acquisition["state"] == "running":
            self.acquisition = {**self.acquisition, "state": self.acquisition_outcome}
        if self.at_preset is None:
            self.at_preset = "spec"
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
