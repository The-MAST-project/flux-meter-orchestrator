"""The Thorlabs Zelux behind the service.

Copied from MAST_unit's `flux_metering/thorcam/thorcam.py`, with stdlib logging and this
service's own `FluxMeterError`; the behavior is unchanged.

**Never exercised against hardware.** The camera was not attached to any machine while this
was written, and `TLCameraSDK()` has never been loaded here -- the SDK's Python package was
installed in a venv, but the native DLLs from the *Scientific Camera Interfaces* bundle
have not been placed or tested. Every member used below was confirmed to exist by
introspecting `thorlabs_tsi_sdk.tl_camera` on 2026-08-30; that they behave as expected is
not yet evidence-backed.

The SDK is imported lazily, inside `open()`, for two reasons: importing it costs a native
library load, and `--fake` must run where the SDK is not installed at all. It is imported
by name through `importlib` because it is not a declared dependency: it ships only inside
Thorlabs' bundle, not on PyPI.

Software-triggered, one frame at a time. The procedure exposes once per spiral step, on its
own thread, alongside an imager exposure -- there is no stream to keep running between
steps, and `frames_per_trigger_zero_for_unlimited = 1` is what makes each `expose()` return
exactly the frame its trigger produced rather than whatever the camera last buffered.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import numpy as np

from flux_meter_service.camera import FluxMeterError

logger = logging.getLogger(__name__)

#: The SDK module that holds `TLCameraSDK`.
SDK_MODULE = "thorlabs_tsi_sdk.tl_camera"

#: How long to wait for a triggered frame beyond the exposure itself. The exposure follows
#: the imager's and can be seconds long, so the poll timeout is derived per configure()
#: rather than fixed; this is the slack added on top for readout and transfer.
POLL_SLACK_MS = 5000

#: `arm()`'s frame-buffer count.
FRAME_BUFFER_COUNT = 2

#: One frame per software trigger.
FRAMES_PER_TRIGGER = 1

MICROSECONDS_PER_MILLISECOND = 1000


class ThorCam:
    """One Zelux, opened by discovery order.

    There is exactly one camera, so the first enumerated device is the one. A selection
    parameter can wait until there is a second.
    """

    def __init__(self) -> None:
        self._sdk: Any = None
        self._camera: Any = None
        self._saturation: int | None = None
        self._description = "ThorCam(unopened)"
        self._model: str | None = None
        self._serial_number: str | None = None

    # ------------------------------------------------------------------ lifecycle --

    def open(self) -> None:
        try:
            sdk_module = importlib.import_module(SDK_MODULE)
        except Exception as ex:  # a missing SDK is a configuration fault, not a crash
            raise FluxMeterError(
                f"the Thorlabs SDK could not be imported: {ex}. The Python package is "
                f"installed from the Scientific Camera Interfaces bundle, and its native "
                f"DLLs must be on the library search path."
            ) from ex

        try:
            self._sdk = sdk_module.TLCameraSDK()
            serials = self._sdk.discover_available_cameras()
        except Exception as ex:
            raise FluxMeterError(f"the Thorlabs SDK would not start: {ex}") from ex

        if not serials:
            # Named possibilities, not just the symptom. The SDK reports an empty list
            # identically for "unplugged", "no driver" and "someone else has it", and the
            # first time this fired the cause was the second: the camera enumerated as a
            # bare `TSI` device with problem code 28 (CM_PROB_FAILED_INSTALL), because the
            # Python wheel ships the user-mode DLLs but NOT the kernel driver. A working
            # install shows `Thorlabs Camera Zelux`, status OK, service CYUSB3.
            raise FluxMeterError(
                "the Thorlabs SDK loaded but found no camera. Either it is not connected, "
                "or ThorCam is not installed so the USB driver is missing (check Device "
                r"Manager for a device on USB\VID_1313 -- problem code 28 means no driver), "
                "or another process already has the camera open."
            )
        if len(serials) > 1:
            # Not an error: say which one was taken, so a second camera appearing does not
            # silently change which one the measurement came from.
            logger.warning(f"{len(serials)} Thorlabs cameras found; opening the first, {serials[0]}")

        try:
            self._camera = self._sdk.open_camera(serials[0])
        except Exception as ex:
            raise FluxMeterError(f"could not open Thorlabs camera '{serials[0]}': {ex}") from ex

        camera = self._camera
        # Full scale from the camera's own bit depth. Deriving it here is what keeps
        # "saturated" meaning the same thing if the camera is reconfigured or replaced.
        self._saturation = (1 << int(camera.bit_depth)) - 1
        # Kept as fields, not parsed back out of `_description`, because they go into the
        # frames' INSTRUME and CAMSN cards and must not depend on that string's shape.
        self._model = str(camera.model)
        self._serial_number = str(camera.serial_number)
        # The ranges go in too, because this string is what `result.json` carries as the
        # run's record of its instrument. A run whose settings were refused, or which sat
        # near a limit, is then explicable from the products alone -- without them a reader
        # has to find the same camera and ask it.
        self._description = (
            f"{camera.model} sn={camera.serial_number} "
            f"{camera.sensor_width_pixels}x{camera.sensor_height_pixels} "
            f"bit_depth={camera.bit_depth} (full scale {self._saturation}) "
            f"exposure_us={tuple(camera.exposure_time_range_us)} "
            f"gain={tuple(camera.gain_range)} "
            f"black_level={tuple(camera.black_level_range)}"
        )
        logger.info(f"opened {self._description}")

    def close(self) -> None:
        for name, obj, method in (("camera", self._camera, "dispose"), ("sdk", self._sdk, "dispose")):
            if obj is None:
                continue
            try:
                getattr(obj, method)()
            except Exception as ex:  # noqa: BLE001 -- teardown must not mask the run's own outcome
                logger.error(f"closing the ThorCam {name} failed: {ex}")
        self._camera = None
        self._sdk = None

    # ----------------------------------------------------------------- operation --

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        """Apply the run's settings, refusing an exposure the camera will not honor.

        The exposure is not chosen for this camera -- it follows the imager's, so that the
        two frames integrate over the same window -- and nothing guarantees the Zelux
        accepts it. A camera that silently clamps would still return plausible frames while
        quietly breaking that property, so the range is checked here and the run refused.
        """
        camera = self._require_camera()

        lo, hi = camera.exposure_time_range_us
        if not (lo <= exposure_us <= hi):
            raise FluxMeterError(
                f"exposure {exposure_us} us is outside the camera's range [{lo}, {hi}] us. "
                f"The flux exposure follows the imager's, so lower `seconds` to fit."
            )

        camera.exposure_time_us = int(exposure_us)
        self._set_within_range(camera, "gain", gain, camera.gain_range)
        self._set_within_range(camera, "black_level", black_level, camera.black_level_range)

        camera.frames_per_trigger_zero_for_unlimited = FRAMES_PER_TRIGGER
        camera.image_poll_timeout_ms = int(exposure_us / MICROSECONDS_PER_MILLISECOND) + POLL_SLACK_MS
        logger.info(
            f"configured: exposure={exposure_us}us gain={gain} black_level={black_level} "
            f"poll_timeout={camera.image_poll_timeout_ms}ms"
        )

    @staticmethod
    def _set_within_range(camera: Any, name: str, value: int, valid_range: tuple[int, int]) -> None:
        lo, hi = valid_range
        if not (lo <= value <= hi):
            raise FluxMeterError(f"{name} {value} is outside the camera's range [{lo}, {hi}]")
        setattr(camera, name, value)

    def expose(self) -> np.ndarray:
        """One software-triggered frame.

        `get_pending_frame_or_null` returns None on timeout rather than raising, so the
        null has to be turned into an error here -- a caller handed None would otherwise
        record a flux of zero and read it as "no light reached the fiber", which is a
        measurement rather than a failure.
        """
        camera = self._require_camera()
        camera.arm(FRAME_BUFFER_COUNT)
        try:
            camera.issue_software_trigger()
            frame = camera.get_pending_frame_or_null()
            if frame is None:
                raise FluxMeterError(f"no frame within {camera.image_poll_timeout_ms} ms of the trigger")
            return np.copy(frame.image_buffer)
        finally:
            # In `finally` because an armed camera refuses the next `arm()`, so a raised
            # exposure would otherwise break every step that follows it, not just its own.
            try:
                camera.disarm()
            except Exception as ex:  # noqa: BLE001
                logger.error(f"disarm failed: {ex}")

    # ----------------------------------------------------------------- properties --

    @property
    def saturation_level(self) -> int:
        if self._saturation is None:
            raise FluxMeterError("the camera is not open, so its saturation level is unknown")
        return self._saturation

    @property
    def description(self) -> str:
        return self._description

    @property
    def model(self) -> str:
        return self._model or "unknown"

    @property
    def serial_number(self) -> str:
        return self._serial_number or "unknown"

    def _require_camera(self) -> Any:
        if self._camera is None:
            raise FluxMeterError("the ThorCam is not open")
        return self._camera
