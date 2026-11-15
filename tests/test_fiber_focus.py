"""The fiber-focus run against a faked unit and service: a settled guide start, every position
exposed and named by where the focuser was, the focuser sent home on every way out, and guiding
left running unless an operator aborted the unit."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from astropy.io import fits

from flux_orchestrator import fiber_focus, unit_client
from flux_orchestrator.fiber_focus import (
    RESULT_FILE,
    FiberFocusParams,
    FiberFocusRecord,
    FiberFocusRun,
    GuideSettle,
    SettleOutcome,
    Terminal,
    frame_name,
    parse_args,
)
from flux_orchestrator.flux_client import FluxMeterApiError, FluxMeterClient
from flux_orchestrator.unit_client import Target, UnitApiError, UnitClient
from tests.fakes import UNIT, Sky

HOME = 20000


@pytest.fixture
def sky(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sky:
    monkeypatch.setattr(unit_client, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(fiber_focus, "POLL_INTERVAL_S", 0.0)
    return Sky(share_root=tmp_path / "share", known_as_good=HOME, focuser_position=HOME)


def make_run(sky: Sky, tmp_path: Path, **overrides) -> FiberFocusRun:
    values: dict = dict(
        unit=UNIT,
        target=Target(ra_j2000_hours=1.0, dec_j2000_degs=60.0),
        step=100,
        count=3,
        flux_seconds=0.01,
        settle_seconds=0.0,
        guide_settle_timeout_seconds=5.0,
    )
    values.update(overrides)
    unit = UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(sky.unit_handler)))
    meter = FluxMeterClient("http://flux", client=httpx.Client(transport=httpx.MockTransport(sky.flux_handler)))
    return FiberFocusRun(FiberFocusParams(**values), unit, meter, output_root=tmp_path / "out")


def recorded(run: FiberFocusRun) -> dict:
    return json.loads((run.folder / RESULT_FILE).read_text())


def settle_of(record: FiberFocusRecord) -> GuideSettle:
    assert record.guide_settle is not None
    return record.guide_settle


def acts(sky: Sky) -> list[str]:
    return [call for call in sky.calls if call != "status"]


# ------------------------------------------------------------------ the pieces --


def test_a_frame_is_named_by_its_index_from_one_and_its_measured_position():
    assert frame_name(0, 20100, 0) == "flux-001-20100-00.fits"
    assert frame_name(11, 9876, 3) == "flux-012-09876-03.fits"


def test_the_cli_wants_a_target_and_a_sweep():
    args = parse_args(
        ["--unit", "mast03", "--flux-meter", "http://x", "--ra", "1.9", "--dec", "28.5", "--step", "-50", "--count", "7"]
    )
    assert (args.step, args.count, args.start) == (-50, 7, None)
    with pytest.raises(SystemExit):
        parse_args(["--unit", "mast03", "--flux-meter", "http://x", "--step", "50", "--count", "3"])


# -------------------------------------------------------------------- a full run --


def test_a_completed_run_exposes_every_position_and_sends_the_focuser_home(sky: Sky, tmp_path: Path):
    run = make_run(sky, tmp_path, start=21000, frames_per_position=2)

    record = run.run()

    assert record.terminal == Terminal.COMPLETED
    assert sky.focuser_moves == [21000, 21100, 21200]
    assert acts(sky) == [
        "start_acquisition_and_guiding",
        "stage/move_to_preset",
        "start_guiding",
        "focuser/position",
        "focuser/position",
        "focuser/position",
        "focuser/goto_known_as_good_position",
    ]
    assert sky.focuser_position == HOME
    assert (record.final_focuser_commanded, record.final_focuser_measured) == (HOME, HOME)
    # Guiding is left running, as the in-unit run left it.
    assert "Guiding" in sky.activities
    assert sky.guiding_record is not None and sky.guiding_record["ended"] is None
    assert settle_of(record).outcome == SettleOutcome.SETTLED
    names = sorted(f.name for f in run.folder.glob("flux-*.fits"))
    assert names == [frame_name(i, 21000 + 100 * i, n) for i in range(3) for n in range(2)]
    header = fits.getheader(run.folder / names[-1])
    assert (header["POSIDX"], header["FRAMEIDX"], header["FOCUSPOS"], header["FOCUSCMD"]) == (3, 1, 21200, 21200)
    assert header["RA"] == pytest.approx(15.0)
    assert header["RUNSEQ"] == run.folder.name
    result = recorded(run)
    assert result["terminal"] == "completed"
    assert result["commanded_positions"] == [21000, 21100, 21200]
    assert [len(p["frames"]) for p in result["positions"]] == [2, 2, 2]


def test_the_sweep_starts_at_known_as_good_position_by_default_even_when_the_focuser_sits_there(sky: Sky, tmp_path: Path):
    """No `Moving` is raised for a move to where the focuser already is; the wait must not hang."""
    record = make_run(sky, tmp_path).run()

    assert record.terminal == Terminal.COMPLETED
    assert record.start == HOME
    assert sky.focuser_moves == [HOME, HOME + 100, HOME + 200]


def test_the_measured_position_names_the_frame(sky: Sky, tmp_path: Path):
    record = make_run(sky, tmp_path, start=21000, count=1).run()

    assert record.positions[0].measured == 21000
    assert (Path(record.folder) / frame_name(0, 21000, 0)).exists()


def test_result_json_is_rewritten_after_every_position(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = make_run(sky, tmp_path, start=21000)
    seen: list[int] = []
    write = run._write_record

    def spy() -> None:
        write()
        seen.append(len(recorded(run)["positions"]))

    monkeypatch.setattr(run, "_write_record", spy)
    run.run()

    assert seen == [1, 2, 3, 3]


# ------------------------------------------------------------- refused up front --


@pytest.mark.parametrize(
    ("overrides", "sky_setup", "why"),
    [
        ({"start": 59950, "step": 100}, {}, "outside the focuser's range"),
        ({"start": 100, "step": -100}, {}, "outside the focuser's range"),
        ({"step": 0}, {}, "a step of 0"),
        ({"count": 1000}, {}, "at most 999"),
        ({}, {"activities": ["Guiding"]}, "busy (Guiding)"),
        ({}, {"activities": ["Acquiring"]}, "busy (Acquiring)"),
        ({"start": None}, {"known_as_good": None}, "no known_as_good_position"),
    ],
)
def test_a_run_that_cannot_work_is_refused_before_anything_moves(sky: Sky, tmp_path: Path, overrides, sky_setup, why):
    for name, value in sky_setup.items():
        setattr(sky, name, value)
    run = make_run(sky, tmp_path, **overrides)

    with pytest.raises(UnitApiError, match=why.replace("(", r"\(").replace(")", r"\)")):
        run.run()

    assert acts(sky) == []
    assert recorded(run)["terminal"] == "failed"


def test_a_second_run_is_refused_while_the_first_left_guiding(sky: Sky, tmp_path: Path):
    make_run(sky, tmp_path).run()
    sky.calls.clear()

    with pytest.raises(UnitApiError, match="busy"):
        make_run(sky, tmp_path).run()

    assert acts(sky) == []


# ---------------------------------------------------------------- the guide start --


@pytest.mark.parametrize(
    ("setup", "outcome", "error"),
    [
        ({"settle": "failed"}, SettleOutcome.FAILED, "Lost star"),
        ({"settle": "ends"}, SettleOutcome.ENDED, "PHD2: not connected"),
        ({"guiding_refused": True}, SettleOutcome.ENDED, "mount is None"),
        ({"guider_backend": "solving"}, SettleOutcome.ENDED, None),
    ],
)
def test_a_guide_start_that_does_not_settle_fails_the_run_and_sends_the_focuser_home(
    sky: Sky, tmp_path: Path, setup, outcome, error
):
    for name, value in setup.items():
        setattr(sky, name, value)

    record = make_run(sky, tmp_path).run()

    assert record.terminal == Terminal.GUIDING_FAILED
    assert settle_of(record).outcome == outcome
    if error is not None:
        assert error in (settle_of(record).error or "")
    assert sky.focuser_moves == []
    assert acts(sky)[-1] == "focuser/goto_known_as_good_position"
    assert "abort" not in sky.calls


def test_no_settle_in_time_is_a_timeout(sky: Sky, tmp_path: Path):
    sky.settle = "never"

    record = make_run(sky, tmp_path, guide_settle_timeout_seconds=0.0).run()

    assert record.terminal == Terminal.GUIDING_FAILED
    assert settle_of(record).outcome == SettleOutcome.TIMEOUT
    assert "abort" not in sky.calls


# ------------------------------------------------------------------- the aborts --


def test_an_operator_abort_mid_sweep_ends_the_run_as_aborted(sky: Sky, tmp_path: Path):
    sky.operator_abort_at_move = 1
    sky.aborting_reads = 3

    record = make_run(sky, tmp_path, start=21000).run()

    assert record.terminal == Terminal.ABORTED
    assert [p.commanded for p in record.positions] == [21000]
    assert sky.focuser_moves == [21000, 21100]
    assert acts(sky)[-1] == "focuser/goto_known_as_good_position"
    assert sky.focuser_position == HOME
    assert "abort" not in sky.calls


def test_an_operator_abort_during_a_move_that_never_ends_is_seen_without_waiting_it_out(
    sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sky.operator_abort_at_move = 1
    sky.aborting_reads = 100
    run = make_run(sky, tmp_path, start=21000)
    # The stall starts with the second move; the first arrives.
    move = run.unit.move_focuser

    def stall_from_the_second(position, interrupted=None):
        if sky.focuser_moves:
            sky.focuser_stuck = True
        return move(position, interrupted=interrupted)

    monkeypatch.setattr(run.unit, "move_focuser", stall_from_the_second)
    monkeypatch.setattr(unit_client, "FOCUSER_TIMEOUT_S", 0.5)
    monkeypatch.setattr(unit_client, "ABORT_SETTLE_TIMEOUT_S", 0.0)

    record = run.run()

    assert record.terminal == Terminal.ABORTED
    assert [p.commanded for p in record.positions] == [21000]


def test_an_operator_abort_while_settling_is_an_abort_not_a_guiding_failure(sky: Sky, tmp_path: Path):
    sky.operator_abort_while_settling = True

    record = make_run(sky, tmp_path).run()

    assert record.terminal == Terminal.ABORTED
    assert settle_of(record).outcome == SettleOutcome.ABORTED
    assert sky.focuser_moves == []


def test_ctrl_c_while_acquiring_aborts_the_unit(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = make_run(sky, tmp_path)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(run.unit, "put_mirror_in", interrupted)
    record = run.run()

    assert record.terminal == Terminal.ABORTED
    assert record.unit_aborted
    assert "abort" in sky.calls
    assert acts(sky)[-1] == "focuser/goto_known_as_good_position"


def test_ctrl_c_mid_sweep_leaves_guiding_running(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = make_run(sky, tmp_path, start=21000)
    expose = run.meter.expose
    calls = iter(range(100))

    def interrupt_on_second():
        if next(calls) == 1:
            raise KeyboardInterrupt
        return expose()

    monkeypatch.setattr(run.meter, "expose", interrupt_on_second)
    record = run.run()

    assert record.terminal == Terminal.ABORTED
    assert not record.unit_aborted and "abort" not in sky.calls
    assert "Guiding" in sky.activities
    assert sky.focuser_position == HOME


def test_an_acquisition_the_unit_failed_is_not_aborted(sky: Sky, tmp_path: Path):
    sky.acquisition_outcome = "failed"

    record = make_run(sky, tmp_path).run()

    assert record.terminal == Terminal.ACQUISITION_FAILED
    assert "abort" not in sky.calls
    assert "start_guiding" not in sky.calls


def test_an_acquisition_this_side_gave_up_on_is_aborted(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sky.acquisition_outcome = "running"
    monkeypatch.setattr(unit_client, "ACQUISITION_TIMEOUT_S", 0.0)
    run = make_run(sky, tmp_path)

    with pytest.raises(UnitApiError, match="timed out"):
        run.run()

    assert recorded(run)["terminal"] == "failed"
    assert run.record.unit_aborted


# ------------------------------------------------------------ failures mid-sweep --


def test_a_flux_meter_failure_mid_sweep_sends_the_focuser_home_without_aborting(
    sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run = make_run(sky, tmp_path, start=21000)

    def broken():
        raise FluxMeterApiError("camera gone")

    monkeypatch.setattr(run.meter, "expose", broken)
    with pytest.raises(FluxMeterApiError):
        run.run()

    assert recorded(run)["terminal"] == "failed"
    assert "abort" not in sky.calls
    assert "Guiding" in sky.activities
    assert sky.focuser_position == HOME


def test_a_focuser_that_never_arrives_fails_the_run(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sky.focuser_stuck = True
    monkeypatch.setattr(unit_client, "FOCUSER_TIMEOUT_S", 0.0)
    run = make_run(sky, tmp_path, start=21000)

    with pytest.raises(UnitApiError, match="timed out"):
        run.run()

    result = recorded(run)
    assert result["terminal"] == "failed"
    assert result["positions"] == []
    assert result["return_error"] is not None


def test_a_refused_return_home_is_recorded_without_masking_the_terminal(sky: Sky, tmp_path: Path):
    sky.home_refused = True

    record = make_run(sky, tmp_path, start=21000).run()

    assert record.terminal == Terminal.COMPLETED
    assert "known_as_good_position is None" in (record.return_error or "")
    assert record.final_focuser_measured is None


# ---------------------------------------------------- what the second review found --


def test_a_refused_acquisition_request_does_not_abort_the_unit(sky: Sky, tmp_path: Path):
    """Another client's acquisition, or a plan holding the unit: nothing of this run's is in flight."""
    sky.acquisition_refused = True
    run = make_run(sky, tmp_path)

    with pytest.raises(UnitApiError, match="already acquiring"):
        run.run()

    assert "abort" not in sky.calls
    assert not run.record.unit_aborted
    assert "focuser/goto_known_as_good_position" not in sky.calls


def test_a_mirror_move_that_times_out_is_not_aborted(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(unit_client, "STAGE_TIMEOUT_S", 0.0)
    run = make_run(sky, tmp_path)

    with pytest.raises(UnitApiError, match="SPEC"):
        run.run()

    assert "abort" not in sky.calls
    assert acts(sky)[-1] == "focuser/goto_known_as_good_position"


def test_an_operator_abort_between_frames_stops_the_frames(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = make_run(sky, tmp_path, start=21000, frames_per_position=4)
    expose = run.meter.expose
    taken = iter(range(100))

    def abort_during_the_second_frame():
        if next(taken) == 1:
            sky.operator_abort()
        return expose()

    monkeypatch.setattr(run.meter, "expose", abort_during_the_second_frame)
    record = run.run()

    assert record.terminal == Terminal.ABORTED
    assert len(list(run.folder.glob("flux-*.fits"))) == 2


def test_an_operator_abort_during_the_last_frame_is_not_completed(sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run = make_run(sky, tmp_path, start=21000, count=1)
    expose = run.meter.expose

    def abort_while_exposing():
        sky.operator_abort()
        return expose()

    monkeypatch.setattr(run.meter, "expose", abort_while_exposing)
    record = run.run()

    assert record.terminal == Terminal.ABORTED


def test_a_failed_read_of_the_records_while_settling_is_waited_out(
    sky: Sky, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    sky.settle_reads = 3
    run = make_run(sky, tmp_path)
    start_guiding = run.unit.start_guiding

    def then_two_failed_reads() -> None:
        start_guiding()
        sky.unreadable_records_reads = 2

    monkeypatch.setattr(run.unit, "start_guiding", then_two_failed_reads)
    record = run.run()

    assert record.terminal == Terminal.COMPLETED


def test_records_that_never_read_time_out_naming_the_units_errors(sky: Sky, tmp_path: Path):
    sky.unreadable_records_reads = 10_000

    record = make_run(sky, tmp_path, guide_settle_timeout_seconds=0.0).run()

    assert record.terminal == Terminal.GUIDING_FAILED
    assert settle_of(record).outcome == SettleOutcome.TIMEOUT
    assert "PWI4 did not answer" in (record.error or "")
