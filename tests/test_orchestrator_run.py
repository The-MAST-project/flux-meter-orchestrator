"""A whole run against a faked unit and service: the right cell, the right shift, and the unit
left as a run must leave it on every way out."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pytest
from astropy.io import fits

from flux_orchestrator import run as run_module
from flux_orchestrator import unit_client
from flux_orchestrator.flux_client import FluxMeterClient
from flux_orchestrator.run import RESULT_FILE, Run, RunParams
from flux_orchestrator.spiral import Terminal
from flux_orchestrator.unit_client import Target, UnitApiError, UnitClient
from tests.fakes import UNIT, Sky

SHIFT_TOLERANCE_PX = 0.6


@pytest.fixture
def sky(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sky:
    monkeypatch.setattr(unit_client, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(run_module, "FRAME_POLL_INTERVAL_S", 0.0)
    return Sky(share_root=tmp_path / "share")


def make_run(sky: Sky, tmp_path: Path, **overrides) -> Run:
    params = RunParams(unit=UNIT, seconds=1.0, target=Target(ra_j2000_hours=1.0, dec_j2000_degs=60.0), **overrides)
    unit = UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(sky.unit_handler)))
    meter = FluxMeterClient("http://flux", client=httpx.Client(transport=httpx.MockTransport(sky.flux_handler)))
    return Run(params, unit, meter, share_root=sky.share_root, output_root=tmp_path / "out")


def recorded(record) -> dict:
    return json.loads((Path(record.folder) / RESULT_FILE).read_text())


def test_a_run_finds_the_peak_cell_and_measures_its_commanded_offset(sky, tmp_path):
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    assert record.terminal == Terminal.CONVERGED
    result = record.result
    assert result is not None
    assert result.argmax_cell == sky.peak_cell
    expected = sky.commanded_px(sky.peak_cell)
    assert result.commanded_offset_px == pytest.approx(expected)
    assert (result.dx, result.dy) == pytest.approx(expected, abs=SHIFT_TOLERANCE_PX), "the field moved as commanded"
    assert result.fiber_source == "fake", "the center comes from the unit's own resolution"


def test_the_unit_is_driven_in_order_and_left_closed(sky, tmp_path):
    make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    order = [c for c in sky.calls if c != "status"]
    assert order[:3] == ["start_acquisition_and_guiding", "stage/move_to_preset", "spiral_new_path"]
    assert order[-1] == "spiral_end_path"
    assert not sky.session_open and not sky.tracking


def test_every_flux_frame_and_the_record_are_written(sky, tmp_path):
    record = make_run(sky, tmp_path, number_of_frames=3, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    document = recorded(record)
    assert document["terminal"] == "converged"
    assert len(document["steps"]) == sky.steps + 1, "the origin is a step too"
    frames = sorted(p.name for p in Path(record.folder).glob("flux-*.fits"))
    assert len(frames) == 3 * len(document["steps"])
    assert document["steps"][0]["imager_frame"].endswith("/reference.fits")


def test_a_failed_acquisition_opens_no_session_and_is_recorded(sky, tmp_path):
    sky.acquisition_outcome = "failed"

    with pytest.raises(UnitApiError, match="acquisition failed"):
        make_run(sky, tmp_path).run()

    assert "spiral_new_path" not in sky.calls
    folders = list((tmp_path / "out").rglob(RESULT_FILE))
    assert len(folders) == 1
    assert json.loads(folders[0].read_text())["terminal"] == "failed"


def test_an_interrupted_walk_closes_the_session(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0)
    real_step = run.unit.spiral_next_step
    taken = []

    def interrupted(seconds):
        taken.append(seconds)
        if len(taken) == 3:
            raise KeyboardInterrupt
        return real_step(seconds)

    monkeypatch.setattr(run.unit, "spiral_next_step", interrupted)
    record = run.run()

    assert record.terminal == Terminal.ABORTED
    assert sky.calls[-1] == "spiral_end_path", "the session must be closed, which stops tracking"
    assert "abort" not in sky.calls, "closing the session is enough; the unit is not aborted mid-session"
    assert recorded(record)["terminal"] == "aborted"


def test_an_arg_max_without_a_frame_gives_no_result_and_says_why(sky, tmp_path):
    sky.failing_step = 2  # (1, -1), the peak cell
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    assert record.result is None
    assert record.no_result_reason is not None and "has no imager frame" in record.no_result_reason


def test_the_frames_must_reach_the_share(sky, tmp_path, monkeypatch):
    """A frame that never arrives is a lost frame, reported as such, not waited on forever."""
    monkeypatch.setattr(run_module, "FRAME_ARRIVAL_TIMEOUT_S", 0.0)
    monkeypatch.setattr(sky, "write_frame", lambda name: f"mast01/2026-10-19/Spirals/0001/{name}")

    with pytest.raises(FileNotFoundError, match="did not arrive"):
        make_run(sky, tmp_path).run()
    assert not sky.session_open, "the session is closed even when the run fails"


def test_a_half_copied_frame_is_not_read_as_a_frame(tmp_path, monkeypatch):
    """The mover copies the frame onto the share, so a read can catch it partway."""
    monkeypatch.setattr(run_module, "FRAME_ARRIVAL_TIMEOUT_S", 0.0)
    path = tmp_path / "step-0001.fits"
    fits.PrimaryHDU(data=np.ones((300, 300), dtype=np.float32)).writeto(path)
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 2])

    with pytest.raises(FileNotFoundError, match="did not arrive"):
        run_module.read_frame(path)
