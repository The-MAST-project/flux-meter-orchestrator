"""A MAST unit, through its public HTTP API only.

The calls follow MAST_common's `docs/scripting-the-api.md`: arguments in the query string, `PUT`
to act and `GET` to read, and the `errors` in the response envelope rather than the HTTP status
deciding success. Every wait is bounded.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

UNIT_PORT = 8000
UNIT_BASE_PATH = "/mast/api/v1/unit"
REQUEST_TIMEOUT_S = 20.0
POLL_INTERVAL_S = 2.0
ERROR_BODY_CHARS = 400

#: A spiral step is answered once the mount has settled and the step's frame is saved:
#: the exposure plus PHD2's save of a full-frame image plus the settle.
STEP_OVERHEAD_S = 120.0
#: An acquisition solves and corrects several times and may resolve a name first.
ACQUISITION_TIMEOUT_S = 900.0
#: A Sky->Spec traverse was measured at 21-22 s on mast02 (2026-09-02); a ceiling, not a tolerance.
STAGE_TIMEOUT_S = 120.0
SPEC_PRESET = "spec"


class UnitApiError(RuntimeError):
    """The unit reported errors, could not be reached, or did not finish in time."""


class AcquisitionState(StrEnum):
    """`FullUnitStatus.latest.acquisition.state`, MAST_common `models/latest.py`."""

    RUNNING = "running"
    GUIDING = "guiding"
    ENDED = "ended"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(frozen=True)
class Target:
    """Where to acquire: coordinates, or a name the unit resolves. Both None means wherever the
    telescope points."""

    ra_j2000_hours: float | None = None
    dec_j2000_degs: float | None = None
    object_name: str | None = None


@dataclass(frozen=True)
class SpiralStep:
    #: None, with `error`, when PWI4 reported no spiral offset for this step.
    cell: tuple[int, int] | None
    ring: int | None
    #: On the share, or None with `image_error` saying why.
    image: str | None
    image_error: str | None
    error: str | None


class UnitClient:
    def __init__(self, host: str, port: int = UNIT_PORT, client: httpx.Client | None = None) -> None:
        self.name = host
        self.base_url = f"http://{host}:{port}{UNIT_BASE_PATH}"
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S, trust_env=False)

    def status(self) -> dict[str, Any]:
        return self._request("GET", "status", {})

    def abort(self) -> None:
        self._request("PUT", "abort", {})

    def acquire(self, target: Target, seconds: float, gain: int | None) -> None:
        """Acquire onto the assumed fiber position and wait for the outcome; no guiding.

        The unit replaces `status.latest.acquisition` with a fresh `RUNNING` record before it
        answers, so the first status read after this PUT already describes this acquisition.
        """
        self._request(
            "PUT",
            "start_acquisition_and_guiding",
            {
                "seconds": seconds,
                "ra_j2000_hours": target.ra_j2000_hours,
                "dec_j2000_degs": target.dec_j2000_degs,
                "object_name": target.object_name,
                "gain_absolute": gain,
                "skip_sky": True,
                "use_set_limit_frame": True,
                "handover_automatically_to_guider": False,
            },
        )
        outcome = self._latest_acquisition(
            self._wait_for(
                "the acquisition to finish",
                ACQUISITION_TIMEOUT_S,
                lambda status: (self._latest_acquisition(status) or {}).get("state") != AcquisitionState.RUNNING,
            )
        )
        if outcome is None:
            raise UnitApiError(f"{self.name}: status reports no latest acquisition; the unit predates it")
        if outcome["state"] != AcquisitionState.ENDED:
            raise UnitApiError(f"{self.name}: acquisition {outcome['state']}: {outcome.get('errors')}")

    def put_mirror_in(self) -> None:
        """Move the folding mirror to SPEC and wait for it to be there. Waits on position, not on
        a moving flag, which reads False for up to a poll period after the move is commanded."""
        if self._stage_preset(self.status()) == SPEC_PRESET:
            return
        self._request("PUT", "stage/move_to_preset", {"preset": SPEC_PRESET})
        self._wait_for("the folding mirror to reach SPEC", STAGE_TIMEOUT_S, lambda s: self._stage_preset(s) == SPEC_PRESET)

    def spiral_new_path(
        self,
        x_step_arcsec: float,
        y_step_arcsec: float,
        exposure_seconds: float,
        usable_fraction: float,
        center: tuple[int, int] | None,
    ) -> dict[str, Any]:
        """Open a spiral session that saves every step's frame. Where they land is in
        `latest_spiral`; the session is open either way."""
        return self._request(
            "PUT",
            "spiral_new_path",
            {
                "x_step_arcsec": x_step_arcsec,
                "y_step_arcsec": y_step_arcsec,
                "exposure_seconds": exposure_seconds,
                "save_intermediate_exposures": True,
                "usable_fraction": usable_fraction,
                "center_x": center[0] if center else None,
                "center_y": center[1] if center else None,
            },
            timeout=exposure_seconds + STEP_OVERHEAD_S,
        )

    def spiral_new_frameless_path(self, x_step_arcsec: float, y_step_arcsec: float) -> dict[str, Any]:
        """Open a spiral session that only moves the mount, leaving the frames to `expose`
        (MAST_unit#385). The session is reported in `latest_spiral`, with `frames` false."""
        return self._request(
            "PUT",
            "spiral_new_path",
            {"x_step_arcsec": x_step_arcsec, "y_step_arcsec": y_step_arcsec, "frames": False},
            timeout=STEP_OVERHEAD_S,
        )

    def spiral_next_step(self, exposure_seconds: float = 0.0) -> SpiralStep:
        """`exposure_seconds` is the step frame's; a frameless session takes none."""
        value = self._request("PUT", "spiral_next_step", {}, timeout=exposure_seconds + STEP_OVERHEAD_S)
        cell = (int(value["cell"][0]), int(value["cell"][1])) if "cell" in value else None
        return SpiralStep(
            cell=cell,
            ring=int(value["ring"]) if cell is not None else None,
            image=value["image"],
            image_error=value.get("image_error"),
            error=value.get("error"),
        )

    def latest_spiral(self) -> dict[str, Any]:
        """`status.latest.spiral`. The unit replaces it before `spiral_new_path` answers, so the
        first read after it describes that session; `shared_products_path` is None while the
        unit's share is demoted."""
        spiral = (self.status().get("latest") or {}).get("spiral")
        if spiral is None:
            raise UnitApiError(f"{self.name}: status reports no latest spiral session; the unit predates it")
        return spiral

    def spiral_end_path(self, exposure_seconds: float = 0.0) -> dict[str, Any]:
        """Close the session. One with frames takes its final frame, correlates it and stops
        tracking; a frameless one leaves tracking as `spiral_new_path` found it."""
        return self._request("PUT", "spiral_end_path", {}, timeout=exposure_seconds + STEP_OVERHEAD_S)

    def expose(self, subfolder: str, exposure_seconds: float, repeats: int, gain: int | None) -> None:
        """Start an exposure series where the mount points, into `subfolder` of the night's
        exposures. Answered before the first frame; the unit replaces `status.latest.exposure`
        before it answers, so `latest_exposure` then describes this series."""
        self._request(
            "PUT",
            "expose",
            {"subfolder": subfolder, "exposure_seconds": exposure_seconds, "repeats": repeats, "gain": gain},
        )

    def latest_exposure(self) -> dict[str, Any]:
        """`status.latest.exposure`: the last `expose` series, with each frame it attempted."""
        exposure = (self.status().get("latest") or {}).get("exposure")
        if exposure is None:
            raise UnitApiError(f"{self.name}: status reports no latest exposure")
        return exposure

    def wait_for_released(self, timeout_s: float) -> None:
        """Wait for `latest.spiral` and `latest.exposure` to say they have ended: `unit/abort`
        answers before it has stopped them."""
        self._wait_for("the spiral session and the exposure series to end", timeout_s, self._released)

    @staticmethod
    def _released(status: dict[str, Any]) -> bool:
        latest = status.get("latest") or {}
        return all(
            record is None or record["ended"] is not None for record in (latest.get("spiral"), latest.get("exposure"))
        )

    @staticmethod
    def _latest_acquisition(status: dict[str, Any]) -> dict[str, Any] | None:
        return (status.get("latest") or {}).get("acquisition")

    @staticmethod
    def _stage_preset(status: dict[str, Any]) -> str | None:
        stage = status.get("stage")
        if stage is None:
            raise UnitApiError("no stage in status: the component did not build")
        return stage.get("at_preset")

    def _wait_for(self, what: str, timeout_s: float, done: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        while True:
            status = self.status()
            if done(status):
                return status
            if time.monotonic() > deadline:
                raise UnitApiError(f"{self.name}: timed out after {timeout_s:g} s waiting for {what}")
            time.sleep(POLL_INTERVAL_S)

    def _request(self, verb: str, method: str, params: dict[str, Any], timeout: float = REQUEST_TIMEOUT_S) -> Any:
        url = f"{self.base_url}/{method}"
        supplied = {name: value for name, value in params.items() if value is not None}
        try:
            response = self._client.request(verb, url, params=supplied, timeout=timeout)
        except httpx.HTTPError as error:
            raise UnitApiError(f"{verb} {url}: {error}") from error
        if response.is_error:
            raise UnitApiError(f"{verb} {url}: HTTP {response.status_code}: {response.text[:ERROR_BODY_CHARS]}")
        body = response.json()
        if not isinstance(body, dict) or "api_version" not in body:
            raise UnitApiError(f"{verb} {url}: not a MAST response envelope: {body!r}")
        if body.get("errors"):
            raise UnitApiError(f"{verb} {url}: {body['errors']}")
        return body.get("value")
