"""A stand-in for the Zelux, for testing the network path without the hardware.

Plain noise about a low background, in the real camera's shape and bit depth: enough for a
unit to configure, expose and receive a frame of the right size, and nothing that looks
like a measurement.
"""

from __future__ import annotations

import numpy as np

from flux_meter_service.camera import FluxMeterError

#: The CS165MU's sensor and ADC, as the unit's `SimulatedFluxMeter` describes them.
SENSOR_WIDTH_PX = 1440
SENSOR_HEIGHT_PX = 1080
BIT_DEPTH = 10

#: The unit simulator's dark background, in counts, with a read-noise-like spread on top.
BACKGROUND_COUNTS = 3.0
NOISE_COUNTS = 1.0

MODEL = "FAKE-CS165MU"
SERIAL_NUMBER = "fake"


class FakeCamera:
    def __init__(self, seed: int | None = None) -> None:
        self._rng = np.random.default_rng(seed)
        self._saturation = (1 << BIT_DEPTH) - 1

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        if exposure_us <= 0:
            raise FluxMeterError(f"exposure_us must be positive, got {exposure_us}")

    def expose(self) -> np.ndarray:
        frame = self._rng.normal(BACKGROUND_COUNTS, NOISE_COUNTS, size=(SENSOR_HEIGHT_PX, SENSOR_WIDTH_PX))
        return np.clip(np.rint(frame), 0, self._saturation).astype(np.uint16)

    @property
    def saturation_level(self) -> int:
        return self._saturation

    @property
    def description(self) -> str:
        return (
            f"FAKE camera, noise only -- not the Zelux. {MODEL} {SENSOR_WIDTH_PX}x{SENSOR_HEIGHT_PX} "
            f"bit_depth={BIT_DEPTH} (full scale {self._saturation})"
        )

    @property
    def model(self) -> str:
        return MODEL

    @property
    def serial_number(self) -> str:
        return SERIAL_NUMBER
