"""The three endpoints, in process, against a stub camera."""

from __future__ import annotations

import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from fastapi.testclient import TestClient

from flux_meter_service.app import create_app
from flux_meter_service.camera import FluxMeterError
from flux_meter_service.fake import FakeCamera

REFUSAL = "exposure 10 us is outside the camera's range [64, 26843418] us"
EXPOSE_FAILURE = "no frame within 5001 ms of the trigger"
SLOW_CALL_S = 0.2
CONCURRENT_CALLS = 2


class StubCamera:
    """A camera that records what it was asked and fails on demand."""

    def __init__(self, frame: np.ndarray | None = None) -> None:
        self.frame = frame if frame is not None else np.arange(12, dtype=np.uint16).reshape(3, 4)
        self.refusal: str | None = None
        self.expose_failure: str | None = None
        self.configured: tuple[int, int, int] | None = None
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        if self.refusal is not None:
            raise FluxMeterError(self.refusal)
        self.configured = (exposure_us, gain, black_level)

    def expose(self) -> np.ndarray:
        if self.expose_failure is not None:
            raise FluxMeterError(self.expose_failure)
        return self.frame

    @property
    def saturation_level(self) -> int:
        return 1023

    @property
    def description(self) -> str:
        return "CS165MU sn=34567 1440x1080 bit_depth=10"

    @property
    def model(self) -> str:
        return "CS165MU"

    @property
    def serial_number(self) -> str:
        return "34567"


class SlowCamera(StubCamera):
    """Every call takes a while and counts how many are in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self._guard = threading.Lock()
        self._in_flight = 0
        self.max_in_flight = 0

    def _slow(self) -> None:
        with self._guard:
            self._in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self._in_flight)
        time.sleep(SLOW_CALL_S)
        with self._guard:
            self._in_flight -= 1

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        self._slow()
        super().configure(exposure_us, gain, black_level)

    def expose(self) -> np.ndarray:
        self._slow()
        return super().expose()


class OpenFails(StubCamera):
    def open(self) -> None:
        raise FluxMeterError("the Thorlabs SDK loaded but found no camera")


def _settings(exposure_us: int = 2_000_000) -> dict[str, int]:
    return {"exposure_us": exposure_us, "gain": 100, "black_level": 12}


def test_the_camera_is_opened_at_startup_and_closed_at_shutdown():
    camera = StubCamera()
    with TestClient(create_app(camera)):
        assert camera.opened
        assert not camera.closed
    assert camera.closed


def test_info_describes_the_camera():
    camera = StubCamera()
    with TestClient(create_app(camera)) as client:
        response = client.get("/info")

    assert response.status_code == 200
    assert response.json() == {
        "model": camera.model,
        "serial_number": camera.serial_number,
        "saturation_level": camera.saturation_level,
        "description": camera.description,
    }


def test_configure_passes_the_settings_to_the_camera():
    camera = StubCamera()
    with TestClient(create_app(camera)) as client:
        response = client.put("/configure", json=_settings())

    assert response.status_code == 200
    assert camera.configured == (2_000_000, 100, 12)


def test_a_refused_configure_is_a_400_carrying_the_cameras_reason():
    camera = StubCamera()
    camera.refusal = REFUSAL
    with TestClient(create_app(camera)) as client:
        response = client.put("/configure", json=_settings(exposure_us=10))

    assert response.status_code == 400
    assert response.json() == {"detail": REFUSAL}


@pytest.mark.parametrize("dtype", [np.uint16, np.uint8, np.float32])
def test_expose_returns_a_frame_that_round_trips(dtype):
    frame = (np.arange(1440 * 1080) % 1023).astype(dtype).reshape(1080, 1440)
    with TestClient(create_app(StubCamera(frame))) as client:
        response = client.post("/expose")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    got = np.load(io.BytesIO(response.content), allow_pickle=False)
    assert got.dtype == frame.dtype
    assert got.shape == frame.shape
    assert np.array_equal(got, frame)


def test_a_camera_failure_on_expose_is_a_503_with_the_reason():
    camera = StubCamera()
    camera.expose_failure = EXPOSE_FAILURE
    with TestClient(create_app(camera)) as client:
        response = client.post("/expose")

    assert response.status_code == 503
    assert response.json() == {"detail": EXPOSE_FAILURE}


def test_camera_calls_never_overlap():
    camera = SlowCamera()
    with TestClient(create_app(camera)) as client, ThreadPoolExecutor(CONCURRENT_CALLS) as pool:
        futures = [pool.submit(client.put, "/configure", json=_settings()), pool.submit(client.post, "/expose")]
        statuses = [f.result().status_code for f in futures]

    assert statuses == [200, 200]
    assert camera.max_in_flight == 1


def test_a_camera_that_will_not_open_stops_startup():
    with pytest.raises(FluxMeterError, match="found no camera"), TestClient(create_app(OpenFails())):
        pass


def test_the_fake_camera_looks_like_the_zelux_and_says_it_is_fake():
    camera = FakeCamera()
    with TestClient(create_app(camera)) as client:
        info = client.get("/info").json()
        assert client.put("/configure", json=_settings()).status_code == 200
        frame = np.load(io.BytesIO(client.post("/expose").content), allow_pickle=False)

    assert "fake" in info["model"].lower()
    assert "fake" in info["description"].lower()
    assert info["saturation_level"] == 1023
    assert frame.shape == (1080, 1440)
    assert frame.dtype == np.uint16
    assert frame.max() <= info["saturation_level"]
    assert frame.std() > 0


def test_the_fake_camera_refuses_a_nonpositive_exposure():
    with TestClient(create_app(FakeCamera())) as client:
        response = client.put("/configure", json=_settings(exposure_us=0))

    assert response.status_code == 400
    assert "exposure" in response.json()["detail"]
