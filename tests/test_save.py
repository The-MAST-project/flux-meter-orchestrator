"""`POST /expose_and_save`: a frame written as FITS under the share, numbered per night."""

from __future__ import annotations

import datetime
from pathlib import Path

import numpy as np
from astropy.io import fits
from fastapi.testclient import TestClient
from test_service import EXPOSE_FAILURE, StubCamera, _settings

from flux_meter_service.app import create_app
from flux_meter_service.save import FRAME_FOLDER, frame_name, observing_night


def _client(camera: StubCamera, root: Path) -> TestClient:
    return TestClient(create_app(camera, save_root=root))


def test_the_frame_is_saved_as_fits_under_the_nights_folder(tmp_path):
    camera = StubCamera()
    with _client(camera, tmp_path) as client:
        response = client.post("/expose_and_save")

    assert response.status_code == 200
    saved = Path(response.json()["path"])
    night = observing_night(datetime.datetime.now(datetime.UTC))
    assert saved == tmp_path / night / FRAME_FOLDER / frame_name(1)
    data = fits.getdata(saved)
    assert isinstance(data, np.ndarray)
    assert np.array_equal(data, camera.frame)
    header = fits.getheader(saved)
    assert header["SEQNUM"] == 1
    assert header["CAMMODEL"] == camera.model
    assert header["CAMSERNO"] == camera.serial_number
    assert header["SATLEVEL"] == camera.saturation_level
    assert "DATE-OBS" in header


def test_frames_are_numbered_in_sequence(tmp_path):
    with _client(StubCamera(), tmp_path) as client:
        paths = [Path(client.post("/expose_and_save").json()["path"]).name for _ in range(3)]

    assert paths == [frame_name(1), frame_name(2), frame_name(3)]


def test_numbering_continues_after_a_restart(tmp_path):
    with _client(StubCamera(), tmp_path) as client:
        client.post("/expose_and_save")
        client.post("/expose_and_save")
    with _client(StubCamera(), tmp_path) as client:
        name = Path(client.post("/expose_and_save").json()["path"]).name

    assert name == frame_name(3)


def test_the_settings_configured_through_the_service_are_recorded(tmp_path):
    with _client(StubCamera(), tmp_path) as client:
        client.put("/configure", json=_settings(exposure_us=2_000_000))
        saved = client.post("/expose_and_save").json()["path"]

    header = fits.getheader(saved)
    assert header["EXPTIME"] == 2.0
    assert header["GAIN"] == 100
    assert header["BLKLEVEL"] == 12


def test_settings_never_configured_here_are_left_out_rather_than_guessed(tmp_path):
    with _client(StubCamera(), tmp_path) as client:
        saved = client.post("/expose_and_save").json()["path"]

    header = fits.getheader(saved)
    assert "EXPTIME" not in header
    assert "GAIN" not in header


def test_a_camera_failure_is_a_503_and_writes_nothing(tmp_path):
    camera = StubCamera()
    camera.expose_failure = EXPOSE_FAILURE
    with _client(camera, tmp_path) as client:
        response = client.post("/expose_and_save")

    assert response.status_code == 503
    assert response.json() == {"detail": EXPOSE_FAILURE}
    assert not list(tmp_path.rglob("*.fits"))


def test_an_unwritable_share_is_a_503_naming_the_path(tmp_path):
    blocked = tmp_path / "not-a-folder"
    blocked.write_text("a file where the root should be")
    with _client(StubCamera(), blocked) as client:
        response = client.post("/expose_and_save")

    assert response.status_code == 503
    assert str(blocked) in response.json()["detail"]
