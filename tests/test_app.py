"""The app, driven headless with Textual's Pilot against the fakes: the live panels, a run set up,
watched and finished, a stop and a hard stop, quitting refused mid-run, and one session per host."""

from __future__ import annotations

import asyncio
import datetime
import functools
import json
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from flux_orchestrator import unit_client
from flux_orchestrator.app import AppLock, AppLockedError, OrchestratorApp, RunScreen, Services, history
from flux_orchestrator.flux_client import FluxMeterClient
from flux_orchestrator.progress import StopToken
from flux_orchestrator.session import Kind, Session
from flux_orchestrator.unit_client import UnitClient
from tests.fakes import UNIT, Sky

HOME = 20000
WAIT_S = 10.0


def in_loop[**P](test: Callable[P, Awaitable[None]]) -> Callable[P, None]:
    """Run an async test in its own event loop, with no async plugin for pytest."""

    @functools.wraps(test)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        asyncio.run(test(*args, **kwargs))  # type: ignore[arg-type]

    return run


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(unit_client, "POLL_INTERVAL_S", 0.0)


class Gate:
    """Holds the run inside its first flux exposure until opened, and says when it got there."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.opened = threading.Event()

    def open(self) -> None:
        self.opened.set()


def services(sky: Sky, panel_sky: Sky, gate: Gate | None = None, status_fails: bool = False) -> Services:
    def flux(request: httpx.Request) -> httpx.Response:
        if gate is not None and request.url.path == "/expose":
            gate.entered.set()
            gate.opened.wait(WAIT_S)
        return sky.flux_handler(request)

    def status_client(_session: Session) -> UnitClient:
        if status_fails:
            raise RuntimeError("an unexpected failure in the status read")
        return UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(panel_sky.unit_handler)))

    return Services(
        unit=lambda s, stop: UnitClient(
            UNIT, client=httpx.Client(transport=httpx.MockTransport(sky.unit_handler)), stop=stop
        ),
        meter=lambda s: FluxMeterClient("http://flux", client=httpx.Client(transport=httpx.MockTransport(flux))),
        # The panel reads a sky of its own: every read of the fake moves it on, and the run counts on that.
        status=status_client,
    )


def make_app(tmp_path: Path, gate: Gate | None = None, status_fails: bool = False) -> tuple[OrchestratorApp, Sky]:
    sky = Sky(share_root=tmp_path / "share", known_as_good=HOME, focuser_position=HOME)
    panel_sky = Sky(share_root=tmp_path / "panel")
    session = Session(
        unit=UNIT, flux_meter="http://flux", output_root=str(tmp_path / "out"), ra_j2000_hours=1.0, dec_j2000_degs=60.0
    )
    for name, value in {
        "start": "21000",
        "step": "100",
        "count": "2",
        "flux_seconds": "0.01",
        "settle_seconds": "0",
    }.items():
        session.set_param(Kind.FIBER_FOCUS, name, value)
    app = OrchestratorApp(session, tmp_path / "session.json", services(sky, panel_sky, gate, status_fails), poll_s=0.05)
    return app, sky


async def until(pilot, condition, what: str) -> None:
    for _ in range(int(WAIT_S / 0.05)):
        if condition():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def result_of(app: OrchestratorApp) -> dict:
    folder = Path(app.run_screen.query_one("#run-folder").render().plain.removeprefix("products: "))  # type: ignore[union-attr]
    return json.loads((folder / "result.json").read_text())


@in_loop
async def test_the_main_page_shows_the_session_the_unit_and_the_camera(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        main = app.main
        assert main is not None
        await until(pilot, lambda: "activities" in str(main.query_one("#unit-panel").render()), "the unit panel")
        await until(pilot, lambda: "FAKE" in str(main.query_one("#camera-panel").render()), "the camera panel")
        assert "mast01" in str(main.query_one("#session-panel").render())


@in_loop
async def test_a_run_set_up_from_the_menu_is_watched_to_its_end_and_lands_in_history(tmp_path: Path):
    app, sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.finished, "the run to end")

        assert "completed" in str(app.screen.query_one("#run-phase").render())
        assert result_of(app)["terminal"] == "completed"
        assert sky.focuser_moves == [21000, 21100]
        await pilot.press("escape")
        assert app.screen is app.main
        saved = json.loads((tmp_path / "session.json").read_text())
        assert saved["overrides"]["fiber_focus"]["count"] == 2
        rows = history(tmp_path / "out", UNIT, datetime.datetime.now(datetime.UTC))
        assert [r.terminal for r in rows] == ["completed"]


@in_loop
async def test_leaving_a_form_with_esc_keeps_nothing(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    gate = Gate()
    app.services = services(Sky(share_root=tmp_path / "x"), Sky(share_root=tmp_path / "y"), gate)
    async with app.run_test() as pilot:
        app.open_run(Kind.SAVE_SPIRALS)
        await pilot.pause()
        app.screen.query_one("#field-rings").value = "1"  # type: ignore[attr-defined]
        await pilot.press("escape")  # back without running: nothing saved
        assert "rings" not in app.session.overrides.get(Kind.SAVE_SPIRALS, {})
    saved = json.loads((tmp_path / "session.json").read_text()) if (tmp_path / "session.json").exists() else {}
    assert "rings" not in saved.get("overrides", {}).get(Kind.SAVE_SPIRALS, {})


@in_loop
async def test_stop_ends_the_run_as_ctrl_c_would_and_quitting_waits_for_it(tmp_path: Path):
    gate = Gate()
    app, sky = make_app(tmp_path, gate)
    async with app.run_test() as pilot:
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.press("y")
        await until(pilot, gate.entered.is_set, "the run to be inside its first exposure")

        app.action_quit_app()
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert app.is_running and app.active is not None

        await pilot.press("s")
        await pilot.press("y")
        gate.open()
        await until(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.finished, "the run to end")

        result = result_of(app)
        assert result["terminal"] == "aborted"
        assert result["stopped_by"] == "the app (stop)"
        assert result["final_focuser_measured"] == HOME
        assert "abort" not in sky.calls


@in_loop
async def test_a_second_stop_is_a_hard_stop_that_aborts_the_unit(tmp_path: Path):
    gate = Gate()
    app, sky = make_app(tmp_path, gate)
    async with app.run_test() as pilot:
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.press("y")
        await until(pilot, gate.entered.is_set, "the run to be inside its first exposure")

        for _ in range(2):
            await pilot.press("s")
            await pilot.press("y")
            await pilot.pause()
        await until(pilot, lambda: "abort" in sky.calls, "the hard stop's abort")
        gate.open()
        await until(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.finished, "the run to end")

        result = result_of(app)
        assert result["terminal"] == "aborted"
        assert result["stopped_by"] == "the app (hard stop)"


@in_loop
async def test_esc_on_a_running_run_page_stays(tmp_path: Path):
    gate = Gate()
    app, _sky = make_app(tmp_path, gate)
    async with app.run_test() as pilot:
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.press("y")
        await until(pilot, gate.entered.is_set, "the run to be inside its first exposure")
        await pilot.press("escape")
        assert isinstance(app.screen, RunScreen)
        app.active.stop.request()  # type: ignore[union-attr]
        gate.open()
        await until(pilot, lambda: app.screen.finished, "the run to end")  # type: ignore[attr-defined]


def test_one_session_per_host(tmp_path: Path):
    first, second = AppLock(tmp_path / "lock"), AppLock(tmp_path / "lock")
    first.acquire()
    try:
        with pytest.raises(AppLockedError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


@in_loop
async def test_a_status_read_that_fails_unexpectedly_is_shown_not_fatal(tmp_path: Path):
    app, _sky = make_app(tmp_path, status_fails=True)
    async with app.run_test() as pilot:
        main = app.main
        assert main is not None
        await until(pilot, lambda: "not reachable" in str(main.query_one("#unit-panel").render()), "the failure shown")
        await pilot.pause(0.2)
        assert app.is_running


# ------------------------------------------------------------- Avery's crashes --


@in_loop
async def test_brackets_typed_into_the_session_are_shown_not_parsed(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.edit_session()
        await pilot.pause()
        app.screen.query_one("#field-object").value = "[/green] [b"  # type: ignore[attr-defined]
        app.screen.query_one("#field-ra").value = ""  # type: ignore[attr-defined]
        app.screen.query_one("#field-dec").value = ""  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause(0.3)
        assert app.is_running
        assert "[/green] [b" in str(app.main.query_one("#session-panel").render())  # type: ignore[union-attr]


@in_loop
async def test_an_out_of_range_sweep_keeps_the_form_open_with_the_reason(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.last_status is not None, "a status read")
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        app.screen.query_one("#field-start").value = "-1"  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert app.is_running and app.active is None
        error = str(app.screen.query_one("#form-error").render())
        assert "range" in error
        assert app.session.overrides[Kind.FIBER_FOCUS]["start"] == 21000


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [("flux_seconds", "nan", "finite"), ("count", "1", "two positions"), ("step", "abc", "whole number")],
)
@in_loop
async def test_a_value_a_run_cannot_take_is_named_and_nothing_is_kept(tmp_path: Path, field: str, value: str, why: str):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        app.screen.query_one(f"#field-{field}").value = value  # type: ignore[attr-defined]
        await pilot.press("enter")
        await pilot.pause()
        assert why in str(app.screen.query_one("#form-error").render())
        assert app.session.overrides[Kind.FIBER_FOCUS].get(field) != value


@pytest.mark.parametrize(("ra", "dec", "why"), [("99", "-999", "RA"), ("1", "-999", "Dec"), ("nan", "1", "RA")])
@in_loop
async def test_an_impossible_target_is_refused_in_the_session_form(tmp_path: Path, ra: str, dec: str, why: str):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.edit_session()
        await pilot.pause()
        app.screen.query_one("#field-ra").value = ra  # type: ignore[attr-defined]
        app.screen.query_one("#field-dec").value = dec  # type: ignore[attr-defined]
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert why in str(app.screen.query_one("#form-error").render())
        assert app.session.ra_j2000_hours == 1.0


@in_loop
async def test_the_start_is_confirmed_with_the_sweep_spelled_out(tmp_path: Path):
    app, sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        await until(pilot, lambda: app.last_status is not None, "a status read")
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        question = str(app.screen.query_one("Static").render())
        assert "Focuser 21000 to 21100" in question and "Guiding is left running" in question
        await pilot.press("n")
        await pilot.pause()
        assert app.active is None and sky.focuser_moves == []


@in_loop
async def test_quitting_while_the_unit_still_guides_asks_first(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        guiding = Sky(share_root=tmp_path / "guiding", activities=["Guiding"])
        app.services.status = lambda _s: UnitClient(
            UNIT, client=httpx.Client(transport=httpx.MockTransport(guiding.unit_handler))
        )
        await until(pilot, lambda: "Guiding" in ((app.last_status or {}).get("activities_verbal") or []), "guiding read")
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running and "still guiding" in str(app.screen.query_one("Static").render())
        await pilot.press("y")
        await pilot.pause()
        assert not app.is_running


@in_loop
async def test_a_finished_fiber_focus_says_guiding_is_still_running(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    async with app.run_test() as pilot:
        app.open_run(Kind.FIBER_FOCUS)
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.press("y")
        await until(pilot, lambda: isinstance(app.screen, RunScreen) and app.screen.finished, "the run to end")
        assert "guiding is still running" in str(app.screen.query_one("#run-phase").render())


def test_a_history_refresh_before_the_columns_exist_adds_them(tmp_path: Path):
    from flux_orchestrator.app import HISTORY_COLUMNS, HistoryRow, MainScreen

    async def body() -> None:
        app, _sky = make_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            main = app.main
            assert isinstance(main, MainScreen)
            table = main.query_one("#history")
            table.clear(columns=True)  # type: ignore[attr-defined]
            main.show_history([HistoryRow("Fiber focus", str(tmp_path / "a/b/0001"), "2026-11-15T20:00:00", "completed")])
            assert len(table.columns) == len(HISTORY_COLUMNS)  # type: ignore[attr-defined]

    asyncio.run(body())


@in_loop
async def test_g_stops_guiding_and_is_offered_only_while_the_unit_guides(tmp_path: Path):
    app, _sky = make_app(tmp_path)
    unit = Sky(share_root=tmp_path / "guiding", activities=["Guiding"])

    def client(_session: Session, stop: StopToken | None = None) -> UnitClient:
        return UnitClient(UNIT, client=httpx.Client(transport=httpx.MockTransport(unit.unit_handler)), stop=stop)

    app.services.unit = client
    app.services.status = client
    async with app.run_test() as pilot:
        main = app.main
        assert main is not None
        await until(pilot, app.unit_guiding, "guiding read")
        assert main.check_action("stop_guiding", ()) is True
        await pilot.press("g")
        await pilot.press("y")
        await until(pilot, lambda: "Guiding" not in unit.activities, "guiding stopped")
        await until(pilot, lambda: not app.unit_guiding(), "the panel to see it")
        assert main.check_action("stop_guiding", ()) is None
        await pilot.press("g")
        await pilot.pause()
        assert app.screen is main
