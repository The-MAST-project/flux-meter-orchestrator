"""The app's hold on a run: events while it goes, and a stop request that each run handles as it
handles Ctrl+C, raised once, so the cleanup the stop sets off still finishes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from flux_orchestrator import run as run_module
from flux_orchestrator import unit_client
from flux_orchestrator.fiber_focus import FiberFocusParams, FiberFocusRun
from flux_orchestrator.fiber_focus import Terminal as FocusTerminal
from flux_orchestrator.flux_client import FluxMeterClient
from flux_orchestrator.progress import EventKind, Reporter, RunEvent, StopKind, StopRequested, StopToken
from flux_orchestrator.run import Run, RunParams
from flux_orchestrator.run import Terminal as RunTerminal
from flux_orchestrator.save_spirals import SpiralsParams, SpiralsRun, SpiralsTerminal
from flux_orchestrator.unit_client import Target, UnitClient
from tests.fakes import UNIT, Sky

HOME = 20000
TARGET = Target(ra_j2000_hours=1.0, dec_j2000_degs=60.0)


@pytest.fixture
def sky(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sky:
    monkeypatch.setattr(unit_client, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(run_module, "FRAME_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(run_module, "FRAME_ARRIVAL_TIMEOUT_S", 2.0)
    return Sky(share_root=tmp_path / "share", known_as_good=HOME, focuser_position=HOME)


def clients(sky: Sky, stop: StopToken) -> tuple[UnitClient, FluxMeterClient]:
    unit = UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(sky.unit_handler)), stop=stop)
    meter = FluxMeterClient("http://flux", client=httpx.Client(transport=httpx.MockTransport(sky.flux_handler)))
    return unit, meter


def stop_when(stop: StopToken, predicate: Callable[[RunEvent], bool], events: list[RunEvent], kind=StopKind.STOP):
    def watch(event: RunEvent) -> None:
        events.append(event)
        if predicate(event):
            stop.request(kind)

    return watch


def focus_run(sky: Sky, tmp_path: Path, stop: StopToken, on_event, **overrides) -> FiberFocusRun:
    values: dict = dict(unit=UNIT, target=TARGET, step=100, count=3, start=21000, flux_seconds=0.01, settle_seconds=0.0)
    values.update(overrides)
    unit, meter = clients(sky, stop)
    return FiberFocusRun(FiberFocusParams(**values), unit, meter, tmp_path / "out", stop=stop, on_event=on_event)


# ------------------------------------------------------------------ the pieces --


def test_a_stop_raises_once_and_a_hard_stop_is_not_downgraded():
    token = StopToken()
    token.check()
    token.request(StopKind.HARD_STOP)
    token.request(StopKind.STOP)

    with pytest.raises(StopRequested):
        token.check()
    token.check()
    assert token.kind is StopKind.HARD_STOP
    assert issubclass(StopRequested, KeyboardInterrupt)


def test_a_failing_watcher_is_ignored_and_events_are_snapshots():
    seen: list[RunEvent] = []

    def watcher(event: RunEvent) -> None:
        seen.append(event)
        raise RuntimeError("display gone")

    detail = {"cell": (1, 0)}
    Reporter("f", watcher).emit(EventKind.STEP, "x", detail=detail)
    detail["cell"] = (9, 9)

    assert seen[0].detail["cell"] == (1, 0)
    with pytest.raises(TypeError):
        seen[0].detail["cell"] = (2, 2)  # type: ignore[index]


# ------------------------------------------------------------------ fiber focus --


def test_fiber_focus_reports_its_phases_and_positions(sky: Sky, tmp_path: Path):
    events: list[RunEvent] = []

    focus_run(sky, tmp_path, StopToken(), events.append).run()

    phases = [e.phase for e in events if e.kind is EventKind.PHASE]
    assert phases == ["acquiring", "mirror", "guiding", "focusing", "returning"]
    steps = [(e.done, e.total) for e in events if e.kind is EventKind.STEP and e.done is not None]
    assert steps == [(1, 3), (2, 3), (3, 3)]
    assert events[-1].kind is EventKind.END and events[-1].detail["terminal"] == "completed"


def test_a_stop_mid_sweep_ends_aborted_and_still_sends_the_focuser_home(sky: Sky, tmp_path: Path):
    stop, events = StopToken(), []
    run = focus_run(sky, tmp_path, stop, stop_when(stop, lambda e: e.kind is EventKind.STEP and e.done == 1, events))

    record = run.run()

    assert record.terminal == FocusTerminal.ABORTED
    assert [p.commanded for p in record.positions] == [21000]
    # The cleanup's own waits ran to the end: the stop was not raised again inside them.
    assert record.final_focuser_measured == HOME and record.return_error is None
    assert record.stopped_by == "the app (stop)"
    assert "abort" not in sky.calls and "Guiding" in sky.activities


def test_a_stop_while_acquiring_aborts_the_unit_as_ctrl_c_does(sky: Sky, tmp_path: Path):
    sky.acquisition_outcome = "running"
    stop, events = StopToken(), []
    run = focus_run(sky, tmp_path, stop, stop_when(stop, lambda e: e.phase == "acquiring", events))

    record = run.run()

    assert record.terminal == FocusTerminal.ABORTED
    assert record.unit_aborted and "abort" in sky.calls


def test_a_watcher_that_fails_does_not_change_how_the_run_ends(sky: Sky, tmp_path: Path):
    def broken(_event: RunEvent) -> None:
        raise RuntimeError("display gone")

    record = focus_run(sky, tmp_path, StopToken(), broken).run()

    assert record.terminal == FocusTerminal.COMPLETED


# ----------------------------------------------------------------- save spirals --


def test_a_stop_mid_walk_aborts_the_spiral_and_confirms_the_abort(sky: Sky, tmp_path: Path):
    stop, events = StopToken(), []
    unit, meter = clients(sky, stop)
    params = SpiralsParams(unit=UNIT, step_seconds=1.0, flux_seconds=1.0, target=TARGET, rings=1)
    run = SpiralsRun(
        params,
        unit,
        meter,
        tmp_path / "out",
        stop=stop,
        on_event=stop_when(stop, lambda e: e.kind is EventKind.STEP and e.done == 2, events),
    )

    record = run.run()

    assert record.terminal == SpiralsTerminal.ABORTED
    assert len(record.cells) == 2
    assert record.unit_aborted and record.unit_abort_confirmed is True
    assert json.loads((Path(record.folder) / "result.json").read_text())["stopped_by"] == "the app (stop)"


# --------------------------------------------------------------------- max flux --


def test_a_stop_mid_walk_of_the_max_flux_run_closes_the_session(sky: Sky, tmp_path: Path):
    stop, events = StopToken(), []
    unit, meter = clients(sky, stop)
    params = RunParams(unit=UNIT, seconds=1.0, target=TARGET)
    run = Run(
        params,
        unit,
        meter,
        tmp_path / "out",
        stop=stop,
        on_event=stop_when(stop, lambda e: e.kind is EventKind.STEP and e.done == 2, events),
    )

    record = run.run()

    assert record.terminal == RunTerminal.ABORTED
    assert len(record.steps) == 2
    assert "spiral_end_path" in sky.calls


def test_a_stop_pressed_during_the_cleanup_does_not_cut_it_short(sky: Sky, tmp_path: Path):
    """The run has left its work: the stop is not raised, and does not claim to have ended it."""
    stop, events = StopToken(), []
    run = focus_run(sky, tmp_path, stop, stop_when(stop, lambda e: e.phase == "returning", events))

    record = run.run()

    assert record.terminal == FocusTerminal.COMPLETED
    assert record.stopped_by is None
    assert record.final_focuser_measured == HOME and record.return_error is None


def test_a_hard_stop_is_recorded_even_when_no_check_raised_it():
    token = StopToken()
    token.request(StopKind.HARD_STOP)
    token.close()

    token.check()
    assert token.ended_by() is StopKind.HARD_STOP
