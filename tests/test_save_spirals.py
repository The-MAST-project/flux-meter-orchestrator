"""The save-spirals run against a faked unit and service: every cell exposed on a frameless
session, every imager frame paired across the two clocks, and the unit left as found on every way
out."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import httpx
import pytest
from astropy.io import fits

from flux_orchestrator import save_spirals, unit_client
from flux_orchestrator.flux_client import FluxMeterClient
from flux_orchestrator.save_spirals import (
    RESULT_FILE,
    ClockOffset,
    SpiralsParams,
    SpiralsRun,
    SpiralsTerminal,
    Window,
    best_overlap,
    parse_args,
    ring_position,
)
from flux_orchestrator.unit_client import Target, UnitApiError, UnitClient
from tests.fakes import UNIT, UNIT_CLOCK_AHEAD_S, Sky

T0 = datetime.datetime(2026, 10, 29, 20, 0, 0, tzinfo=datetime.UTC)


def at(seconds: float) -> datetime.datetime:
    return T0 + datetime.timedelta(seconds=seconds)


@pytest.fixture
def sky(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sky:
    monkeypatch.setattr(unit_client, "POLL_INTERVAL_S", 0.0)
    return Sky(share_root=tmp_path / "share")


def make_run(sky: Sky, tmp_path: Path, **overrides) -> SpiralsRun:
    params = SpiralsParams(
        unit=UNIT, step_seconds=1.0, flux_seconds=1.0, target=Target(ra_j2000_hours=1.0, dec_j2000_degs=60.0), **overrides
    )
    unit = UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(sky.unit_handler)))
    meter = FluxMeterClient("http://flux", client=httpx.Client(transport=httpx.MockTransport(sky.flux_handler)))
    return SpiralsRun(params, unit, meter, output_root=tmp_path / "out")


def recorded(run: SpiralsRun) -> dict:
    return json.loads((run.folder / RESULT_FILE).read_text())


def aborted(sky: Sky) -> bool:
    """The unit was aborted last, and only read after."""
    if "abort" not in sky.calls:
        return False
    after = sky.calls[len(sky.calls) - sky.calls[::-1].index("abort") :]
    return all(call == "status" for call in after)


# ------------------------------------------------------------------ the pieces --


@pytest.mark.parametrize(
    ("index", "position"), [(0, (0, 1)), (1, (1, 1)), (8, (1, 8)), (9, (2, 1)), (24, (2, 16)), (25, (3, 1))]
)
def test_ring_position_counts_rings_from_the_origin_and_steps_from_one(index, position):
    assert ring_position(index) == position


def test_the_clock_offset_is_bracketed_by_the_round_trip():
    offset = ClockOffset.bracketed(unit_time=at(12.0), sent=at(9.9), answered=at(10.1))

    assert offset.seconds == pytest.approx(2.0)
    assert offset.uncertainty_seconds == pytest.approx(0.1)
    assert offset.to_local(at(12.0)) == at(10.0)


def test_the_best_overlap_is_the_largest():
    flux = [Window(at(0), at(5)), Window(at(5), at(10)), Window(at(10), at(15))]

    assert best_overlap(Window(at(6), at(11)), flux) == (1, pytest.approx(4.0))


def test_no_overlap_pairs_nothing():
    assert best_overlap(Window(at(20), at(25)), [Window(at(0), at(5))]) == (None, 0.0)


# -------------------------------------------------------------------- the run --


def test_a_run_exposes_every_cell_of_its_rings_and_stops(sky, tmp_path):
    run = make_run(sky, tmp_path, rings=1, number_of_frames=2)
    record = run.run()

    assert record.terminal == SpiralsTerminal.COMPLETED
    assert len(record.cells) == 9
    assert sky.steps == 8, "the mount never steps beyond the last ring"
    assert all(len(cell.pairs) == 2 for cell in record.cells)
    assert [cell.cell for cell in record.cells][:3] == [(0, 0), (1, 0), (1, -1)]


def test_the_unit_is_driven_through_a_frameless_session_and_left_as_found(sky, tmp_path):
    make_run(sky, tmp_path, rings=1).run()

    order = [c for c in sky.calls if c != "status"]
    assert order[:4] == ["start_acquisition_and_guiding", "stage/move_to_preset", "spiral_new_path", "expose"]
    assert order[-1] == "spiral_end_path"
    assert "abort" not in order
    assert not sky.session_open
    assert not sky.tracking, "tracking is left as it was before the session"


def test_each_cell_exposes_into_its_own_subfolder(sky, tmp_path):
    run = make_run(sky, tmp_path, rings=1, number_of_frames=3, gain=120)
    run.run()

    subfolders = [p["subfolder"] for p in sky.expose_params]
    assert len(set(subfolders)) == 9
    assert subfolders[1].endswith(f"-{run.folder.name}-1-001")
    assert all(p["repeats"] == "3" and p["gain"] == "120" for p in sky.expose_params)


def test_every_imager_frame_is_paired_with_a_flux_frame(sky, tmp_path):
    run = make_run(sky, tmp_path, rings=1, number_of_frames=2)
    record = run.run()

    for cell in record.cells:
        names = {f.name for f in cell.flux_frames}
        assert all(pair.flux_frame in names for pair in cell.pairs)
        assert cell.shared_folder is not None and cell.shared_folder.endswith(cell.subfolder)
    on_disk = sorted(p.name for p in run.folder.glob("flux-*.fits"))
    assert on_disk == sorted(f.name for cell in record.cells for f in cell.flux_frames if f.kept)


def test_the_clock_offset_is_measured_at_every_cell(sky, tmp_path):
    record = make_run(sky, tmp_path, rings=1).run()

    for cell in record.cells:
        assert cell.clock_offset is not None
        assert abs(cell.clock_offset.seconds - UNIT_CLOCK_AHEAD_S) <= cell.clock_offset.uncertainty_seconds + 0.01


def test_a_guide_frame_handed_back_is_flagged(sky, tmp_path):
    sky.guiding = True
    record = make_run(sky, tmp_path, rings=0, number_of_frames=2).run()

    first, second = record.cells[0].pairs
    assert first.predates_request and first.flux_frame is None
    assert not second.predates_request


def test_a_failed_imager_frame_is_recorded_and_the_walk_goes_on(sky, tmp_path):
    sky.failing_frame = 1
    record = make_run(sky, tmp_path, rings=1, number_of_frames=2).run()

    assert record.terminal == SpiralsTerminal.COMPLETED
    failed = record.cells[0].pairs[1]
    assert failed.imager_error == "PHD2 saved no file" and failed.flux_frame is None


def test_the_flux_frames_say_where_they_were_taken(sky, tmp_path):
    run = make_run(sky, tmp_path, rings=1)
    record = run.run()

    cell = record.cells[2]
    kept = next(f for f in cell.flux_frames if f.kept)
    header = fits.getheader(run.folder / kept.name)
    assert (header["CELLX"], header["CELLY"], header["RING"]) == (1, -1, 1)
    assert (header["NAMERING"], header["RINGSTEP"]) == (1, 2)
    assert header["IMGDIR"] == cell.subfolder


def test_the_record_is_rewritten_after_every_cell(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path, rings=1)
    real_step = run.unit.spiral_next_step
    seen = []

    def stepping():
        seen.append(len(recorded(run)["cells"]))
        return real_step()

    monkeypatch.setattr(run.unit, "spiral_next_step", stepping)
    run.run()

    assert seen == list(range(1, 9))


# ---------------------------------------------------------- every way out --


def test_a_unit_without_frameless_sessions_is_refused_and_aborted(sky, tmp_path):
    sky.predates_frameless = True

    with pytest.raises(UnitApiError, match="MAST_unit#385"):
        make_run(sky, tmp_path).run()

    assert "expose" not in sky.calls
    assert aborted(sky) and not sky.session_open


def test_a_unit_whose_spiral_record_has_no_ra_step_is_refused_and_aborted(sky, tmp_path):
    sky.predates_latest_ra_step = True

    with pytest.raises(UnitApiError, match="commanded RA step"):
        make_run(sky, tmp_path).run()

    assert "expose" not in sky.calls
    assert aborted(sky) and not sky.session_open


def test_the_record_keeps_the_ra_step_the_unit_commanded(sky, tmp_path):
    record = make_run(sky, tmp_path, rings=0).run()

    assert record.spiral is not None and record.spiral["ra_step_commanded_arcsec"] > record.params.x_step_arcsec


def test_a_unit_without_frame_records_is_refused(sky, tmp_path):
    sky.predates_frame_records = True
    run = make_run(sky, tmp_path)

    with pytest.raises(UnitApiError, match="MAST_unit#327"):
        run.run()

    assert aborted(sky) and not sky.session_open
    assert recorded(run)["terminal"] == "failed"


def test_a_demoted_share_fails_the_cell_at_once(sky, tmp_path):
    sky.share_unmapped = True

    with pytest.raises(UnitApiError, match="share is demoted"):
        make_run(sky, tmp_path, number_of_frames=3).run()

    assert aborted(sky)
    assert sky.exposure is not None and len(sky.exposure["frames"]) < 3, "the series was cut short"


def test_a_series_that_never_ends_times_out(sky, tmp_path, monkeypatch):
    monkeypatch.setattr(save_spirals, "FRAME_OVERHEAD_S", -1.0)
    monkeypatch.setattr(sky, "_advance_exposure", lambda: None)

    with pytest.raises(UnitApiError, match="did not end in time"):
        make_run(sky, tmp_path).run()

    assert aborted(sky)
    assert sky.exposure is not None and sky.exposure["succeeded"] is False, "the abort ended the series too"


def test_an_interrupted_walk_aborts_the_unit(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path, rings=1)
    real_step = run.unit.spiral_next_step
    taken = []

    def interrupted():
        taken.append(1)
        if len(taken) == 3:
            raise KeyboardInterrupt
        return real_step()

    monkeypatch.setattr(run.unit, "spiral_next_step", interrupted)
    record = run.run()

    assert record.terminal == SpiralsTerminal.ABORTED
    assert record.unit_aborted and aborted(sky)
    assert not sky.session_open and not sky.tracking
    assert recorded(run)["terminal"] == "aborted"
    assert len(recorded(run)["cells"]) == 3


def test_a_failure_before_the_unit_is_asked_anything_leaves_it_alone(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path)

    def refused(settings):
        raise RuntimeError("the camera refused the settings")

    monkeypatch.setattr(run.meter, "configure", refused)
    with pytest.raises(RuntimeError, match="refused"):
        run.run()

    assert sky.calls == []
    assert recorded(run)["unit_aborted"] is False


def test_a_failed_abort_is_recorded_without_hiding_the_failure(sky, tmp_path, monkeypatch):
    sky.predates_frameless = True
    run = make_run(sky, tmp_path)

    def dead():
        raise UnitApiError("the unit went away")

    monkeypatch.setattr(run.unit, "abort", dead)
    with pytest.raises(UnitApiError, match="MAST_unit#385"):
        run.run()

    assert recorded(run)["unit_abort_error"] == "the unit went away"
    assert recorded(run)["unit_abort_confirmed"] is None


def test_the_abort_is_confirmed_from_the_unit_s_records(sky, tmp_path):
    sky.predates_frame_records = True
    run = make_run(sky, tmp_path)

    with pytest.raises(UnitApiError, match="MAST_unit#327"):
        run.run()

    assert recorded(run)["unit_abort_confirmed"] is True
    assert sky.calls[-1] == "status"


def test_an_abort_the_unit_does_not_act_on_is_recorded_unconfirmed(sky, tmp_path, monkeypatch):
    monkeypatch.setattr(save_spirals, "ABORT_CONFIRM_S", 0.0)
    sky.predates_frame_records = True
    sky.abort_lags = True
    run = make_run(sky, tmp_path)

    with pytest.raises(UnitApiError, match="MAST_unit#327"):
        run.run()

    assert recorded(run)["unit_abort_confirmed"] is False
    assert recorded(run)["error"].endswith("(MAST_unit#327)"), "the original failure is what the run reports"


def test_a_session_that_will_not_close_fails_the_run_and_aborts(sky, tmp_path):
    sky.end_path_refused = True
    run = make_run(sky, tmp_path, rings=1)

    with pytest.raises(UnitApiError, match="spiral_end_path: refused"):
        run.run()

    assert recorded(run)["terminal"] == "failed"
    assert aborted(sky) and not sky.session_open


def test_an_interruption_while_closing_aborts_the_unit(sky, tmp_path, monkeypatch):
    run = make_run(sky, tmp_path, rings=1)

    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr(run.unit, "spiral_end_path", interrupted)
    record = run.run()

    assert record.terminal == SpiralsTerminal.ABORTED
    assert aborted(sky) and not sky.session_open


def test_a_series_that_saved_nothing_fails_the_run(sky, tmp_path):
    sky.failing_series = True
    run = make_run(sky, tmp_path, rings=1)

    with pytest.raises(UnitApiError, match="PHD2 is not connected"):
        run.run()

    assert len(sky.expose_params) == 1, "the walk stopped at the first cell"
    assert recorded(run)["terminal"] == "failed"
    assert aborted(sky)


# ------------------------------------------------------------------ the CLI --


def test_the_cli_defaults_follow_the_params():
    args = parse_args(["--unit", "mast01", "--flux-meter", "http://localhost:8003"])

    assert (args.rings, args.frames, args.step_seconds) == (2, 1, 5.0)


def test_the_cli_refuses_rings_the_frame_names_cannot_hold(capsys):
    with pytest.raises(SystemExit):
        parse_args(["--unit", "mast01", "--flux-meter", "http://x", "--rings", "10"])

    assert "--rings" in capsys.readouterr().err


def test_another_callers_series_is_not_taken_for_this_cells(sky, tmp_path, monkeypatch):
    real_expose = sky._expose

    def someone_elses(params):
        return real_expose(params | {"subfolder": "someone-else"})

    monkeypatch.setattr(sky, "_expose", someone_elses)
    with pytest.raises(UnitApiError, match="not this cell's"):
        make_run(sky, tmp_path).run()


def test_a_cell_beyond_the_last_ring_ends_the_walk_unexposed(sky, tmp_path, monkeypatch):
    """PWI4 owns the traversal; the cell count is only the backstop."""
    real_new_path = sky._spiral_new_path

    def skipping(params):
        response = real_new_path(params)
        sky._cells = iter([(1, 0), (2, 0)])
        return response

    monkeypatch.setattr(sky, "_spiral_new_path", skipping)
    record = make_run(sky, tmp_path, rings=1).run()

    assert record.terminal == SpiralsTerminal.COMPLETED
    assert [cell.cell for cell in record.cells] == [(0, 0), (1, 0)]
    assert len(sky.expose_params) == 2


def test_each_cells_imager_frames_are_moved_in_beside_its_flux_frames(sky, tmp_path):
    """The in-unit run's layout: step-<ring>-<step>-<frame>.fits beside flux-<ring>-<step>-<frame>.fits."""
    record = make_run(sky, tmp_path, rings=1, number_of_frames=2).run()

    folder = Path(record.folder)
    names = sorted(p.name for p in folder.glob("*.fits"))
    assert "step-0-001-00.fits" in names and "step-0-001-01.fits" in names
    assert "flux-0-001-00.fits" in names
    assert record.cells[0].pairs[0].step_frame == "step-0-001-00.fits"
    assert record.cells[0].shared_folder is not None
    assert not Path(record.cells[0].shared_folder).exists()


def test_one_flux_frame_is_kept_per_imager_frame_named_after_it(sky, tmp_path):
    """The in-unit run's layout: flux-<ring>-<step>-<frame> beside step-<ring>-<step>-<frame>, one each;
    the flux frames taken around them are deleted."""
    record = make_run(sky, tmp_path, rings=1, number_of_frames=2).run()

    folder = Path(record.folder)
    names = sorted(p.name for p in folder.glob("*.fits"))
    assert not [n for n in names if n.startswith("capture-")]
    for cell in record.cells:
        ring, within = save_spirals.ring_position(cell.index)
        for n in range(2):
            assert f"flux-{ring}-{within:03d}-{n:02d}.fits" in names
            assert f"step-{ring}-{within:03d}-{n:02d}.fits" in names
        assert [p.flux_frame for p in cell.pairs] == [f"flux-{ring}-{within:03d}-{n:02d}.fits" for n in range(2)]
    assert len([n for n in names if n.startswith("flux-")]) == len([n for n in names if n.startswith("step-")])
