"""What the service needs of a camera, and the one error a camera raises."""

from __future__ import annotations

from typing import Protocol

import numpy as np


class FluxMeterError(Exception):
    """The camera could not be opened, configured, or read.

    Its message is the reason the service hands back to the unit as `detail`, so it is
    written for the operator reading a failed run, not for a stack trace.
    """


class Camera(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None: ...

    def expose(self) -> np.ndarray: ...

    @property
    def saturation_level(self) -> int: ...

    @property
    def description(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def serial_number(self) -> str: ...
