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
    monkeypatch.setattr(run_module, "FRAME_ARRIVAL_TIMEOUT_S", 2.0)
    return Sky(share_root=tmp_path / "share")


def make_run(sky: Sky, tmp_path: Path, **overrides) -> Run:
    params = RunParams(unit=UNIT, seconds=1.0, target=Target(ra_j2000_hours=1.0, dec_j2000_degs=60.0), **overrides)
    unit = UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(sky.unit_handler)))
    meter = FluxMeterClient("http://flux", client=httpx.Client(transport=httpx.MockTransport(sky.flux_handler)))
    return Run(params, unit, meter, output_root=tmp_path / "out")


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


def test_a_unit_with_ra_coordinate_spiral_steps_is_refused_and_its_session_closed(sky, tmp_path):
    """Before MAST_unit#136's fix the unit's RA step is cos(dec) short on the sky, and says nothing."""
    sky.predates_on_sky_steps = True

    with pytest.raises(UnitApiError, match="on-sky spiral steps"):
        make_run(sky, tmp_path).run()

    assert "spiral_next_step" not in sky.calls
    assert sky.calls[-1] == "spiral_end_path", "the session it opened is closed"


def test_a_unit_without_the_latest_records_fails_the_acquisition_at_once(sky, tmp_path, monkeypatch):
    """A unit from before MAST_unit#371 reports `status.acquisition`, not `status.latest`."""
    monkeypatch.setattr(sky, "_status", lambda: {"acquisition": {"state": "ended"}, "stage": {"at_preset": "sky"}})

    with pytest.raises(UnitApiError, match="no latest acquisition"):
        make_run(sky, tmp_path).run()

    assert sky.calls.count("status") == 1, "failed on the first read, not after the acquisition timeout"
    assert "spiral_new_path" not in sky.calls


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


def test_the_camera_is_recorded_as_an_object(sky, tmp_path):
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()
    assert recorded(record)["camera"]["saturation_level"] == 1023


def test_a_failed_final_frame_still_gives_the_runs_result(sky, tmp_path):
    """The unit's final frame is for the record only; the session closes either way."""
    sky.end_fails = True
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    assert record.result is not None and record.result.argmax_cell == sky.peak_cell
    assert record.unit_end_path_error is not None and "final exposure failed" in record.unit_end_path_error


def test_the_record_is_written_when_closing_the_session_fails_too(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0)

    def dead(seconds):
        raise UnitApiError("the unit went away")

    monkeypatch.setattr(run.unit, "spiral_next_step", dead)
    monkeypatch.setattr(run.unit, "spiral_end_path", dead)
    with pytest.raises(UnitApiError, match="went away"):
        run.run()

    document = json.loads((run.folder / RESULT_FILE).read_text())
    assert document["terminal"] == "failed"
    assert document["unit_end_path_error"] == "the unit went away"
    assert len(document["steps"]) == 1


def test_a_unit_that_does_not_report_its_spiral_session_is_refused_and_the_session_closed(sky, tmp_path):
    """Before MAST_unit#349 the session's share folder is nowhere in status."""
    sky.predates_latest_spiral = True

    with pytest.raises(UnitApiError, match="latest spiral"):
        make_run(sky, tmp_path).run()

    assert "spiral_next_step" not in sky.calls
    assert sky.calls[-1] == "spiral_end_path"


def test_a_session_whose_frames_miss_the_share_is_closed(sky, tmp_path):
    """The unit opens the session, and starts tracking, before status says the share is demoted."""
    sky.share_unmapped = True
    with pytest.raises(UnitApiError, match="will not reach the share"):
        make_run(sky, tmp_path).run()
    assert sky.calls[-1] == "spiral_end_path"


def test_an_interruption_before_the_spiral_aborts_the_unit(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(run.unit, "put_mirror_in", interrupted)
    record = run.run()

    assert record.terminal == Terminal.ABORTED
    # The abort, then only reads: the mirror was at a preset, so nothing was moved.
    after = sky.calls[sky.calls.index("abort") + 1 :]
    assert after and all(call == "status" for call in after)
    assert "spiral_new_path" not in sky.calls


def test_an_interruption_after_the_session_closed_leaves_the_unit_alone(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0)

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(run, "_center", interrupted)
    record = run.run()

    assert record.terminal == Terminal.ABORTED
    assert "abort" not in sky.calls
    assert sky.calls.count("spiral_end_path") == 1


def test_a_step_without_a_cell_does_not_end_the_walk(sky, tmp_path):
    sky.cellless_step = 4
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    assert record.terminal == Terminal.CONVERGED
    assert record.steps[4].cell is None and record.steps[4].cell_error is not None
    assert record.result is not None and record.result.argmax_cell == sky.peak_cell


def test_an_arg_max_at_the_origin_is_not_correlated_against_itself(sky, tmp_path):
    sky.peak_cell = (0, 0)
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0).run()

    result = record.result
    assert result is not None and result.argmax_at_reference
    assert (result.dx, result.dy) == (0.0, 0.0)
    assert result.confidence is None, "nothing was correlated, so there is no confidence to report"


def test_the_walk_stops_at_the_radius_cap(sky, tmp_path):
    sky.peak_cell = (2, 2)  # brightening outward, so the ring rule keeps going and the cap stops it
    record = make_run(sky, tmp_path, x_step_arcsec=4.0, y_step_arcsec=2.0, max_radius_arcsec=4.0).run()
    assert record.terminal == Terminal.MAX_RADIUS
