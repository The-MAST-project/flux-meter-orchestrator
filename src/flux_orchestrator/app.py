"""The flux orchestrator as an interactive terminal app (Textual).

    python -m flux_orchestrator.app [--unit mast03] [--flux-meter http://mast-ns-spec:8003] [--session FILE]

One session at a time per host (a lock beside the session file). The main page shows the session,
the unit and the flux camera live, the three runs and this night's history. A run executes on a
worker thread and reports through `progress.RunEvent`s; [s] asks it to stop, as Ctrl+C does on the
command line, and a second [s] also sends `unit/abort` (a hard stop) for a call that would not
return. The app does nothing else to the unit: other actions go through the unit's API directly.

A run is ended without its cleanup if the process dies, which is what happens to an ssh session's
processes when the connection drops: start long runs from the unit's desktop (NoMachine).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList, ProgressBar, RichLog, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from flux_meter_service.save import observing_night
from flux_orchestrator import fiber_focus, save_spirals
from flux_orchestrator import run as max_flux
from flux_orchestrator.flux_client import FluxMeterClient
from flux_orchestrator.progress import EventKind, RunEvent, StopKind, StopToken
from flux_orchestrator.session import SESSION_FILE, TITLES, Kind, Session, SessionError
from flux_orchestrator.unit_client import UnitApiError, UnitClient, is_active

logger = logging.getLogger(__name__)

#: One per host, wherever the app is started from and whichever session file it uses.
LOCK_PATH = Path.home() / ".flux-orchestrator.lock"
STATUS_POLL_S = 2.0
RUN_FOLDERS: dict[Kind, str] = {
    Kind.SAVE_SPIRALS: save_spirals.RUN_FOLDER,
    Kind.FIBER_FOCUS: fiber_focus.RUN_FOLDER,
    Kind.MAX_FLUX: max_flux.RUN_FOLDER,
}
RUNS: dict[Kind, type] = {
    Kind.SAVE_SPIRALS: save_spirals.SpiralsRun,
    Kind.FIBER_FOCUS: fiber_focus.FiberFocusRun,
    Kind.MAX_FLUX: max_flux.Run,
}
#: A run's exposure-like parameters, which must be positive.
POSITIVE = ("seconds", "step_seconds", "acquisition_seconds", "flux_seconds", "number_of_frames", "frames_per_position")


class AppLockedError(RuntimeError):
    """Another session of the app holds this host's lock."""


class AppLock:
    """An exclusive lock on a file for the app's lifetime. The operating system drops it when the
    process ends, however it ends, so a dead session never holds it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: IO[str] | None = None

    def acquire(self) -> None:
        handle = self.path.open("a+")
        try:
            _lock(handle)
        except OSError as ex:
            handle.close()
            raise AppLockedError(f"another flux-orchestrator session holds {self.path}") from ex
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        self._file = handle

    def release(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ------------------------------------------------------------------ the services --


@dataclass
class Services:
    """How the app reaches the unit and the camera; the tests put fakes here."""

    #: The run's client (with its stop token), and the hard stop's.
    unit: Callable[[Session, StopToken | None], UnitClient]
    meter: Callable[[Session], FluxMeterClient]
    #: The live panel's, a client of its own beside the run's.
    status: Callable[[Session], UnitClient]

    @classmethod
    def real(cls) -> Services:
        return cls(
            unit=lambda s, stop: UnitClient(s.unit, port=s.unit_port, stop=stop),
            meter=lambda s: FluxMeterClient(s.flux_meter),
            status=lambda s: UnitClient(s.unit, port=s.unit_port),
        )


def validate(kind: Kind, params: Any) -> str | None:
    """What the app refuses before a run starts; the runs' own checks still apply after it."""
    for name in POSITIVE:
        value = getattr(params, name, None)
        if value is not None and value <= 0:
            return f"{name} must be positive"
    if kind is Kind.SAVE_SPIRALS and not 0 <= params.rings <= save_spirals.MAX_RINGS:
        return f"rings must be 0..{save_spirals.MAX_RINGS}"
    if kind is Kind.FIBER_FOCUS:
        if not 1 <= params.count <= fiber_focus.MAX_POSITIONS:
            return f"count must be 1..{fiber_focus.MAX_POSITIONS}"
        if params.count > 1 and params.step == 0:
            return "a step of 0 would expose the same position again and again"
    return None


def describe_unit(status: dict[str, Any]) -> str:
    mount, focuser, stage = status.get("mount") or {}, status.get("focuser") or {}, status.get("stage") or {}
    covers = (status.get("covers") or {}).get("state_verbal")
    guiding = ((status.get("latest") or {}).get("guiding")) or {}
    lines = [
        f"operational  {status.get('operational')}",
        f"activities   {', '.join(status.get('activities_verbal') or []) or 'idle'}",
        f"covers       {covers}",
        f"mirror       {stage.get('at_preset') or stage.get('position')}",
        f"tracking     {mount.get('tracking')}  RA {mount.get('ra_j2000_hours')}  Dec {mount.get('dec_j2000_degs')}",
        f"focuser      {focuser.get('position')}{'  moving' if is_active(focuser, 'Moving') else ''}",
        f"guide settle {guiding.get('settle') or ('ended' if guiding.get('ended') else 'none')}",
    ]
    why = status.get("why_not_operational")
    if why:
        lines.append(f"why not      {'; '.join(map(str, why))}")
    return "\n".join(lines)


@dataclass
class HistoryRow:
    kind: str
    folder: str
    started: str
    terminal: str


def history(output_root: Path, now: datetime.datetime) -> list[HistoryRow]:
    """This observing night's runs on the share, newest first, whoever started them."""
    night = output_root / observing_night(now)
    rows: list[HistoryRow] = []
    for kind, leaf in RUN_FOLDERS.items():
        for result in sorted((night / leaf).glob(f"*/{max_flux.RESULT_FILE}")):
            try:
                record = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append(
                HistoryRow(
                    TITLES[kind], str(result.parent), record.get("started_at") or "", record.get("terminal") or "running"
                )
            )
    return sorted(rows, key=lambda r: r.started, reverse=True)


# ------------------------------------------------------------------ messages --


class RunEventPosted(Message):
    def __init__(self, event: RunEvent) -> None:
        super().__init__()
        self.event = event


class LogLine(Message):
    def __init__(self, line: str) -> None:
        super().__init__()
        self.line = line


class UnitStatus(Message):
    def __init__(self, text: str, ok: bool) -> None:
        super().__init__()
        self.text = text
        self.ok = ok


class RunFinished(Message):
    def __init__(self, kind: Kind, folder: str | None, terminal: str, error: str | None) -> None:
        super().__init__()
        self.kind = kind
        self.folder = folder
        self.terminal = terminal
        self.error = error


class AppLogHandler(logging.Handler):
    """The runs' log lines, to the run page; posted, so a run thread never waits on the display."""

    def __init__(self, app: App) -> None:
        super().__init__(logging.INFO)
        self.app = app
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.app.post_message(LogLine(self.format(record)))
        except Exception:  # noqa: BLE001 -- a log line must never fail the code that logged it
            self.handleError(record)


# ------------------------------------------------------------------ screens --


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "No"), Binding("y", "dismiss(True)", "Yes")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Label(self.question)
            with Horizontal():
                yield Button("Yes [y]", id="yes", variant="error")
                yield Button("No [esc]", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


@dataclass
class FormField:
    name: str
    label: str
    value: str
    placeholder: str = ""


class Form(Screen[dict[str, str] | None]):
    """One input per field; Ctrl+S saves, Esc goes back without saving."""

    BINDINGS = [Binding("ctrl+s", "save", "Save"), Binding("escape", "cancel", "Back")]

    def __init__(self, title: str, fields: list[FormField], note: str = "") -> None:
        super().__init__()
        self.form_title = title
        self.fields = fields
        self.note = note

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(f"[b]{self.form_title}[/b]  {self.note}")
            for f in self.fields:
                yield Label(f.label)
                yield Input(value=f.value, placeholder=f.placeholder, id=f"field-{f.name}")
            yield Static("", id="form-error")
        yield Footer()

    def show_error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(f"[red]{message}[/red]")

    def action_save(self) -> None:
        self.dismiss({f.name: self.query_one(f"#field-{f.name}", Input).value for f in self.fields})

    def action_cancel(self) -> None:
        self.dismiss(None)


class RunScreen(Screen):
    """One run, live. [s] stops it; a second [s] is a hard stop. Esc goes back once it has ended."""

    BINDINGS = [
        Binding("s", "stop", "Stop"),
        Binding("ctrl+c", "stop", "Stop", priority=True),
        Binding("escape", "back", "Back to main"),
    ]

    def __init__(self, kind: Kind) -> None:
        super().__init__()
        self.kind = kind
        self.finished = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"[b]{TITLES[self.kind]}[/b]", id="run-title")
        yield Static("starting", id="run-phase")
        yield ProgressBar(id="run-progress", show_eta=False)
        yield Static("", id="run-last")
        yield Static("", id="run-folder")
        yield RichLog(id="run-log", wrap=True, max_lines=2000)
        yield Footer()

    def show_event(self, event: RunEvent) -> None:
        self.query_one("#run-folder", Static).update(f"products: {event.folder}")
        if event.kind is EventKind.PHASE:
            self.query_one("#run-phase", Static).update(f"phase: [b]{event.phase}[/b]")
        elif event.kind is EventKind.STEP:
            if event.done is not None:
                bar = self.query_one("#run-progress", ProgressBar)
                bar.update(total=event.total, progress=event.done)
            self.query_one("#run-last", Static).update(event.message)
        elif event.kind is EventKind.FRAME:
            self.query_one("#run-last", Static).update(f"frame {event.done}/{event.total}: {event.message}")
        self.query_one("#run-log", RichLog).write(f"[{event.kind}] {event.message}")

    def show_end(self, finished: RunFinished) -> None:
        self.finished = True
        colour = "green" if finished.terminal in ("completed", "converged") else "yellow"
        text = f"ended: [{colour}][b]{finished.terminal}[/b][/{colour}]"
        if finished.error:
            text += f"  {finished.error}"
        self.query_one("#run-phase", Static).update(text + "   [dim]esc: back to main[/dim]")

    def action_stop(self) -> None:
        self.app.request_stop()  # type: ignore[attr-defined]

    def action_back(self) -> None:
        if self.finished:
            self.app.pop_screen()
        else:
            self.notify("The run is still going: [s] to stop it first.", severity="warning")


class MainScreen(Screen):
    BINDINGS = [
        Binding("e", "edit_session", "Session"),
        Binding("enter", "open_run", "Set up run", show=False),
        Binding("h", "refresh_history", "History"),
        Binding("q", "quit_app", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left"):
                yield Static(id="session-panel")
                yield Static("unit: not read yet", id="unit-panel")
                yield Static("flux camera: not read yet", id="camera-panel")
            with Vertical(id="right"):
                yield Label("[b]Runs[/b]  (enter: set up and run)")
                yield OptionList(*(Option(TITLES[k], id=k.value) for k in Kind), id="runs")
                yield Label("[b]Tonight[/b]")
                yield DataTable(id="history", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#history", DataTable)
        table.add_columns("Run", "Started", "Ended as", "Folder")
        self.refresh_session()

    def refresh_session(self) -> None:
        s: Session = self.app.session  # type: ignore[attr-defined]
        target = (
            f"RA {s.ra_j2000_hours} h, Dec {s.dec_j2000_degs}°"
            if s.ra_j2000_hours is not None
            else (s.object_name or "where the telescope points")
        )
        ready = s.why_not_ready()
        self.query_one("#session-panel", Static).update(
            f"[b]Session[/b]  (e: edit)\nunit         {s.unit or '—'}:{s.unit_port}\nflux meter   {s.flux_meter or '—'}\n"
            f"target       {target}\noutput root  {s.output_root}\n"
            + (f"[red]not ready: {ready}[/red]" if ready else "[green]ready[/green]")
        )

    def show_history(self, rows: list[HistoryRow]) -> None:
        table = self.query_one("#history", DataTable)
        table.clear()
        for row in rows:
            table.add_row(row.kind, row.started[11:19], row.terminal, row.folder)

    def action_edit_session(self) -> None:
        self.app.edit_session()  # type: ignore[attr-defined]

    def action_open_run(self) -> None:
        option = self.query_one("#runs", OptionList).highlighted_option
        if option is not None and option.id is not None:
            self.app.open_run(Kind(option.id))  # type: ignore[attr-defined]

    @on(OptionList.OptionSelected, "#runs")
    def run_chosen(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.app.open_run(Kind(event.option.id))  # type: ignore[attr-defined]

    def action_refresh_history(self) -> None:
        self.app.refresh_history()  # type: ignore[attr-defined]

    def action_quit_app(self) -> None:
        self.app.action_quit_app()  # type: ignore[attr-defined]


# ------------------------------------------------------------------ the app --


@dataclass
class Active:
    kind: Kind
    stop: StopToken
    folder: str | None = None


class OrchestratorApp(App):
    TITLE = "flux orchestrator"
    CSS = """
    #left { width: 1fr; }
    #right { width: 1fr; }
    #session-panel, #unit-panel, #camera-panel { border: round $accent; padding: 0 1; height: auto; }
    #runs { height: 5; }
    #confirm { width: 60; height: auto; border: thick $error; padding: 1 2; background: $surface; }
    Confirm { align: center middle; }
    #run-log { height: 1fr; border: round $accent; }
    """
    BINDINGS = [Binding("ctrl+c", "quit_app", "Quit", show=False, priority=False)]

    def __init__(self, session: Session, session_path: Path, services: Services, poll_s: float = STATUS_POLL_S) -> None:
        super().__init__()
        self.session = session
        self.session_path = session_path
        self.services = services
        self.poll_s = poll_s
        self.active: Active | None = None
        self.run_screen: RunScreen | None = None
        self.main: MainScreen | None = None
        self._log_handler: AppLogHandler | None = None

    def on_mount(self) -> None:
        self.main = MainScreen()
        self.push_screen(self.main)
        self._log_handler = AppLogHandler(self)
        logging.getLogger("flux_orchestrator").addHandler(self._log_handler)
        logging.getLogger("flux_orchestrator").setLevel(logging.INFO)
        self.poll_status()
        self.read_camera()
        self.refresh_history()

    def on_unmount(self) -> None:
        if self._log_handler is not None:
            logging.getLogger("flux_orchestrator").removeHandler(self._log_handler)

    # -- the session --

    def edit_session(self) -> None:
        s = self.session
        fields = [
            FormField("unit", "unit host", s.unit, "mast03"),
            FormField("unit_port", "unit port", str(s.unit_port)),
            FormField("flux_meter", "flux-meter URL", s.flux_meter, "http://mast-ns-spec:8003"),
            FormField("output_root", "output root", s.output_root),
            FormField(
                "ra", "RA J2000, decimal hours (empty: none)", "" if s.ra_j2000_hours is None else str(s.ra_j2000_hours)
            ),
            FormField("dec", "Dec J2000, decimal degrees", "" if s.dec_j2000_degs is None else str(s.dec_j2000_degs)),
            FormField("object", "or an object name the unit resolves", s.object_name or ""),
        ]
        self.push_screen(Form("Session", fields), self._session_edited)

    def _session_edited(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        try:
            s = self.session
            s.unit = values["unit"].strip()
            if any(c in s.unit for c in ":/ "):
                raise ValueError(f"'{s.unit}' is not a bare host name; the port has its own field")
            s.unit_port = int(values["unit_port"])
            s.flux_meter = values["flux_meter"].strip()
            s.output_root = values["output_root"].strip()
            s.ra_j2000_hours = float(values["ra"]) if values["ra"].strip() else None
            s.dec_j2000_degs = float(values["dec"]) if values["dec"].strip() else None
            s.object_name = values["object"].strip() or None
        except ValueError as ex:
            self.notify(f"not saved: {ex}", severity="error")
            return
        self.session.save(self.session_path)
        if self.main is not None:
            self.main.refresh_session()
        self.read_camera()

    # -- a run --

    def open_run(self, kind: Kind) -> None:
        if self.active is not None:
            self.notify("A run is going.", severity="warning")
            return
        fields = [
            FormField(
                f.name,
                f"{f.name} ({f.type_name}){'  *' if f.changed else ''}",
                "" if f.value is None else _shown(f.value),
                f"default {f.default}",
            )
            for f in self.session.fields(kind)
        ]
        note = "ctrl+s: save and run · esc: back"
        self.push_screen(Form(f"{TITLES[kind]}: parameters", fields, note), lambda values: self._run_edited(kind, values))

    def _run_edited(self, kind: Kind, values: dict[str, str] | None) -> None:
        if values is None:
            return
        try:
            for name, text in values.items():
                self.session.set_param(kind, name, text)
        except SessionError as ex:
            self.notify(f"not saved: {ex}", severity="error")
            return
        self.session.save(self.session_path)
        why = self.session.why_not_ready() or validate(kind, self.session.params(kind))
        if why is not None:
            self.notify(f"not started: {why}", severity="error")
            return
        self.start_run(kind)

    def start_run(self, kind: Kind) -> None:
        self.active = Active(kind, StopToken())
        self.run_screen = RunScreen(kind)
        self.push_screen(self.run_screen)
        self._run_worker(kind, self.active.stop)

    @work(thread=True, group="run")
    def _run_worker(self, kind: Kind, stop: StopToken) -> None:
        folder: str | None = None
        terminal, error = "failed", None
        run = None
        try:
            run = RUNS[kind](
                self.session.params(kind),
                self.services.unit(self.session, stop),
                self.services.meter(self.session),
                output_root=Path(self.session.output_root),
                stop=stop,
                on_event=lambda event: self.post_message(RunEventPosted(event)),
            )
            folder = str(run.folder)
            record = run.run()
            terminal, error = str(record.terminal), record.error
        except BaseException as ex:  # noqa: BLE001 -- a run's failure ends up on the run page, never kills the app
            # A run records how it ended before it re-raises; a failure before it existed has no record.
            if run is not None and run.record.terminal is not None:
                terminal = str(run.record.terminal)
            error = f"{type(ex).__name__}: {ex}"
            logger.exception("the run failed")
        self.post_message(RunFinished(kind, folder, terminal, error))

    def on_run_event_posted(self, message: RunEventPosted) -> None:
        if self.active is not None:
            self.active.folder = message.event.folder
        if self.run_screen is not None:
            self.run_screen.show_event(message.event)

    def on_log_line(self, message: LogLine) -> None:
        if self.run_screen is not None and self.active is not None:
            self.run_screen.query_one("#run-log", RichLog).write(message.line)

    def on_run_finished(self, message: RunFinished) -> None:
        self.active = None
        if self.run_screen is not None:
            self.run_screen.show_end(message)
        self.refresh_history()
        self.read_camera()

    def request_stop(self) -> None:
        active = self.active
        if active is None:
            return
        if active.stop.kind is None:
            self.push_screen(Confirm("Stop the run? It ends as Ctrl+C would end it."), self._stop_confirmed)
        else:
            self.push_screen(
                Confirm("Hard stop: send unit/abort now? This also stops guiding and tracking."), self._hard_stop_confirmed
            )

    def _stop_confirmed(self, yes: bool | None) -> None:
        if yes and self.active is not None:
            self.active.stop.request(StopKind.STOP)
            self.notify("Stopping at the next check. [s] again for a hard stop.")

    def _hard_stop_confirmed(self, yes: bool | None) -> None:
        if yes and self.active is not None:
            self.active.stop.request(StopKind.HARD_STOP)
            self._send_abort()

    @work(thread=True, group="abort")
    def _send_abort(self) -> None:
        try:
            self.services.unit(self.session, None).abort()
        except UnitApiError as ex:
            logger.error(f"hard stop: unit/abort failed: {ex}")

    # -- the live panels --

    @work(thread=True, group="status", exclusive=True)
    def poll_status(self) -> None:
        worker = get_current_worker()
        while not worker.is_cancelled:
            if self.session.unit:
                try:
                    status = self.services.status(self.session).status()
                    self.post_message(UnitStatus(describe_unit(status), ok=True))
                except Exception as ex:  # noqa: BLE001 -- shown on the panel; a poller that raised would end the app, run and all
                    self.post_message(UnitStatus(f"not reachable: {type(ex).__name__}: {ex}", ok=False))
            time.sleep(self.poll_s)

    def on_unit_status(self, message: UnitStatus) -> None:
        title = f"[b]Unit {self.session.unit}[/b]" + ("" if message.ok else "  [red]![/red]")
        self._update_panel("#unit-panel", f"{title}\n{message.text}")

    def _update_panel(self, selector: str, text: str) -> None:
        """A panel, once the main page has composed it; a read that lands before is shown by the next."""
        if self.main is not None:
            for panel in self.main.query(selector).results(Static):
                panel.update(text)

    @work(thread=True, group="camera", exclusive=True)
    def read_camera(self) -> None:
        """Once, and after each run: `/info` waits behind an exposure, and what it says never changes."""
        if not self.session.flux_meter:
            return
        try:
            info = self.services.meter(self.session).info()
            text = f"[b]Flux camera[/b]\n{info.model}  sn {info.serial_number}  full scale {info.saturation_level}"
        except Exception as ex:  # noqa: BLE001 -- shown on the panel, whatever the service did
            text = f"[b]Flux camera[/b]  [red]not reachable[/red]\n{ex}"
        self.call_from_thread(self._show_camera, text)

    def _show_camera(self, text: str) -> None:
        self._update_panel("#camera-panel", text)

    @work(thread=True, group="history", exclusive=True)
    def refresh_history(self) -> None:
        rows = history(Path(self.session.output_root), datetime.datetime.now(datetime.UTC))
        self.call_from_thread(self._show_history, rows)

    def _show_history(self, rows: list[HistoryRow]) -> None:
        if self.main is not None and self.main.query("#history"):
            self.main.show_history(rows)

    def action_quit_app(self) -> None:
        if self.active is not None:
            self.notify("A run is going: stop it first ([s] on its page).", severity="warning")
            return
        self.exit()

    async def action_quit(self) -> None:
        """Textual's own quit, which Ctrl+Q and the command palette reach: refused mid-run too."""
        self.action_quit_app()


def _shown(value: Any) -> str:
    return " ".join(map(str, value)) if isinstance(value, tuple) else str(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="flux_orchestrator.app", description="The flux orchestrator, interactive.")
    parser.add_argument("--session", type=Path, default=Path.cwd() / SESSION_FILE, help="the session file")
    parser.add_argument("--unit", help="set the session's unit")
    parser.add_argument("--flux-meter", help="set the session's flux-meter URL")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lock = AppLock(LOCK_PATH)
    try:
        lock.acquire()
    except AppLockedError as ex:
        print(ex, file=sys.stderr)
        return 1
    try:
        session = Session.load(args.session)
        if args.unit:
            session.unit = args.unit
        if args.flux_meter:
            session.flux_meter = args.flux_meter
        session.save(args.session)
        OrchestratorApp(session, args.session, Services.real()).run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
