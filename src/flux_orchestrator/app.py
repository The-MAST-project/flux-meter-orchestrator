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
import copy
import datetime
import json
import logging
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from rich.text import Text
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
HISTORY_COLUMNS = ("Run", "Started UTC", "Ended as", "Folder")
STATUS_POLL_S = 2.0
POLL_CHECK_S = 0.1
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


def validate(kind: Kind, params: Any, focuser: dict[str, Any] | None = None) -> str | None:
    """What the app refuses before a run starts; the runs' own checks still apply after it.
    `focuser` is the unit's last-read focuser status, for the sweep's limits."""
    for name in POSITIVE:
        value = getattr(params, name, None)
        if value is not None and (not math.isfinite(value) or value <= 0):
            return f"{label_of(name)} must be a positive number"
    if kind is Kind.SAVE_SPIRALS and not 0 <= params.rings <= save_spirals.MAX_RINGS:
        return f"rings must be 0 to {save_spirals.MAX_RINGS}"
    if kind is Kind.FIBER_FOCUS:
        if not 2 <= params.count <= fiber_focus.MAX_POSITIONS:
            return f"count must be 2 to {fiber_focus.MAX_POSITIONS}: a sweep needs at least two positions"
        if params.step == 0:
            return "step must not be 0: every position would be the same"
        start = params.start if params.start is not None else (focuser or {}).get("known_as_good_position")
        lower, upper = (focuser or {}).get("lower_limit"), (focuser or {}).get("upper_limit")
        if start is not None:
            end = start + params.step * (params.count - 1)
            low, high = min(start, end), max(start, end)
            if (lower is not None and low < lower) or (upper and high >= upper):
                return f"the sweep {start} to {end} leaves the focuser's range {lower} to {upper}"
    return None


#: What a parameter is, in words, with its units; the form's labels.
LABELS: dict[str, str] = {
    "start": "first focuser position (steps; empty: the unit's known-good position)",
    "step": "focuser step between positions (steps; negative sweeps inward)",
    "count": "number of focuser positions",
    "frames_per_position": "flux frames at each position",
    "settle_seconds": "wait after each focuser move (s)",
    "guide_settle_timeout_seconds": "longest wait for guiding to settle (s)",
    "acquisition_seconds": "acquisition exposure (s)",
    "flux_seconds": "flux camera exposure (s)",
    "step_seconds": "imager exposure at each cell (s)",
    "seconds": "imager and flux exposure (s)",
    "gain": "imager gain (empty: the unit's)",
    "flux_gain": "flux camera gain",
    "flux_black_level": "flux camera black level",
    "x_step_arcsec": "RA step (arcsec on the sky)",
    "y_step_arcsec": "Dec step (arcsec on the sky)",
    "rings": "spiral rings to walk",
    "number_of_frames": "frames at each cell",
    "max_rings": "most rings to walk",
    "patience_rings": "rings past the best before stopping",
    "max_radius_arcsec": "largest radius (arcsec)",
    "usable_fraction": "usable fraction of frames",
    "center": "fiber position on the imager (x y; empty: the unit's)",
    "skip_acquisition": "skip the acquisition (engineering only: true/false)",
}


def label_of(name: str) -> str:
    return LABELS.get(name, name.replace("_", " "))


def summary(kind: Kind, params: Any, session: Session, focuser: dict[str, Any] | None) -> str:
    """The run in one paragraph, for the confirmation before it starts."""
    target = (
        f"RA {session.ra_j2000_hours} h, Dec {session.dec_j2000_degs}°"
        if session.ra_j2000_hours is not None
        else (session.object_name or "where the telescope points")
    )
    lines = [f"{TITLES[kind]} on {session.unit}, target {target}."]
    if kind is Kind.FIBER_FOCUS:
        start = params.start if params.start is not None else (focuser or {}).get("known_as_good_position")
        home = (focuser or {}).get("known_as_good_position")
        if start is not None:
            end = start + params.step * (params.count - 1)
            lines.append(f"Focuser {start} to {end}, {params.count} positions {abs(params.step)} apart (known-good {home}).")
        lines.append(f"{params.frames_per_position} flux frame(s) of {params.flux_seconds:g} s at each position.")
        lines.append("Guiding is left running at the end.")
    else:
        changed = [f.name for f in session.fields(kind) if f.changed]
        if changed:
            lines.append(
                "Changed from the defaults: " + ", ".join(f"{label_of(n)} = {_shown(getattr(params, n))}" for n in changed)
            )
    return "\n".join(lines)


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
    def __init__(self, text: str, ok: bool, status: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.text = text
        self.ok = ok
        self.status = status


class RunFinished(Message):
    def __init__(self, kind: Kind, folder: str | None, terminal: str, error: str | None, guiding_left: bool) -> None:
        super().__init__()
        self.kind = kind
        self.folder = folder
        self.terminal = terminal
        self.error = error
        #: The run started guiding and nothing stopped it: a fiber-focus run that got that far.
        self.guiding_left = guiding_left


class AppLogHandler(logging.Handler):
    """The runs' log lines, to the run page; posted, so a run thread never waits on the display."""

    def __init__(self, app: App) -> None:
        super().__init__(logging.INFO)
        self.app = app
        formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", "%H:%M:%S")
        formatter.converter = time.gmtime
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.app.post_message(LogLine(self.format(record)))
        except Exception:  # noqa: BLE001 -- a log line must never fail the code that logged it
            self.handleError(record)


# ------------------------------------------------------------------ screens --


class Confirm(ModalScreen[bool]):
    BINDINGS = [
        Binding("escape", "dismiss(False)", "No"),
        Binding("n", "dismiss(False)", "No"),
        Binding("y", "dismiss(True)", "Yes"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm"):
            yield Static(Text(self.question))
            with Horizontal():
                yield Button("Yes [y]", id="yes", variant="error")
                yield Button("No [n]", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

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
    """One input per field. Enter or Ctrl+S saves, if `check` passes; a value it refuses keeps the
    form open with the reason under it. Esc goes back without saving."""

    BINDINGS = [Binding("ctrl+s", "save", "Save"), Binding("escape", "cancel", "Back")]

    def __init__(
        self,
        title: str,
        fields: list[FormField],
        note: str = "",
        check: Callable[[dict[str, str]], str | None] | None = None,
    ) -> None:
        super().__init__()
        self.form_title = title
        self.fields = fields
        self.note = note
        self.check = check

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            yield Static(Text.assemble((self.form_title, "bold"), "  ", self.note))
            yield Static("", id="form-error")
            for f in self.fields:
                yield Label(Text(f.label))
                yield Input(value=f.value, placeholder=f.placeholder, id=f"field-{f.name}")
        yield Footer()

    def on_mount(self) -> None:
        inputs = self.query(Input)
        if inputs:
            inputs.first().focus()

    def show_error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(Text(f"Not saved: {message}", style="bold red"))

    @on(Input.Submitted)
    def submitted(self) -> None:
        self.action_save()

    def action_save(self) -> None:
        values = {f.name: self.query_one(f"#field-{f.name}", Input).value for f in self.fields}
        why = self.check(values) if self.check is not None else None
        if why is not None:
            self.show_error(why)
            return
        self.dismiss(values)

    def action_cancel(self) -> None:
        self.dismiss(None)


class RunScreen(Screen):
    """One run, live. [s] stops it; a second [s] is a hard stop. Esc goes back once it has ended."""

    BINDINGS = [
        Binding("s", "stop", "Stop"),
        Binding("ctrl+c", "stop", "Stop", priority=True),
        Binding("q", "quit", "Quit", show=False),
        Binding("escape", "back", "Back to main"),
    ]

    def __init__(self, kind: Kind) -> None:
        super().__init__()
        self.kind = kind
        self.finished = False
        self.phase = "starting"
        self.started = time.monotonic()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(Text(TITLES[self.kind], style="bold"), id="run-title")
        yield Static("starting", id="run-phase")
        yield ProgressBar(id="run-progress", show_eta=False)
        yield Static("", id="run-last")
        yield Static("", id="run-folder")
        yield RichLog(id="run-log", wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)

    def _set(self, selector: str, content: Text | str) -> None:
        """Update a widget if the page still has it: an event can land as the page closes."""
        for widget in self.query(selector).results(Static):
            widget.update(content)

    def _tick(self) -> None:
        if not self.finished:
            elapsed = int(time.monotonic() - self.started)
            self._set(
                "#run-phase",
                Text.assemble("phase: ", (self.phase, "bold"), f"   {elapsed // 60}:{elapsed % 60:02d} elapsed"),
            )

    def show_event(self, event: RunEvent) -> None:
        self._set("#run-folder", Text(f"products: {event.folder}"))
        if event.kind is EventKind.PHASE:
            self.phase = event.phase or ""
            self._tick()
            for log in self.query("#run-log").results(RichLog):
                log.write(Text(f"--- {event.phase}", style="bold"))
        elif event.kind is EventKind.STEP:
            if event.done is not None:
                for bar in self.query("#run-progress").results(ProgressBar):
                    bar.update(total=event.total, progress=event.done)
            self._set("#run-last", Text(event.message))
        elif event.kind is EventKind.FRAME:
            self._set("#run-last", Text(f"frame {event.done}/{event.total}: {event.message}"))

    def show_end(self, finished: RunFinished) -> None:
        self.finished = True
        good = finished.terminal in ("completed", "converged")
        text = Text.assemble("ended: ", (finished.terminal, "bold green" if good else "bold yellow"))
        if finished.error:
            text.append(f"  {finished.error}")
        if finished.guiding_left:
            text.append(
                "\nguiding is still running on the unit; stop it through the unit's API "
                "(stop_acquisition_and_guiding) before the next run",
                style="yellow",
            )
        text.append("\nesc: back to main", style="dim")
        self._set("#run-phase", text)

    def action_stop(self) -> None:
        self.app.request_stop()  # type: ignore[attr-defined]

    def action_quit(self) -> None:
        self.app.action_quit_app()  # type: ignore[attr-defined]

    def action_back(self) -> None:
        if self.finished:
            self.app.pop_screen()
        else:
            self.notify("The run is still going: s or Ctrl+C to stop it first.", severity="warning", markup=False)


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
        if not table.columns:
            table.add_columns(*HISTORY_COLUMNS)
        runs = self.query_one("#runs", OptionList)
        last = self.app.session.last_run  # type: ignore[attr-defined]
        runs.highlighted = [k.value for k in Kind].index(last) if last in [k.value for k in Kind] else 0
        self.refresh_session()

    def refresh_session(self) -> None:
        s: Session = self.app.session  # type: ignore[attr-defined]
        target = (
            f"RA {s.ra_j2000_hours} h, Dec {s.dec_j2000_degs}°"
            if s.ra_j2000_hours is not None
            else (s.object_name or "where the telescope points")
        )
        ready = s.why_not_ready()
        panel = Text.assemble(
            ("Session", "bold"),
            "  (e: edit)\n",
            f"unit         {s.unit or '—'}  port {s.unit_port}\nflux meter   {s.flux_meter or '—'}\n",
            f"target       {target}\noutput root  {s.output_root}\n",
            (f"not ready: {ready}", "red") if ready else ("ready", "green"),
        )
        for widget in self.query("#session-panel").results(Static):
            widget.update(panel)

    def show_history(self, rows: list[HistoryRow]) -> None:
        table = self.query_one("#history", DataTable)
        if not table.columns:
            # A refresh can land before on_mount has added them.
            table.add_columns(*HISTORY_COLUMNS)
        table.clear()
        for row in rows:
            table.add_row(row.kind, row.started[11:19], row.terminal, "/".join(Path(row.folder).parts[-3:]))

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
        #: The last status the panel read, for the focuser's limits and whether the unit is guiding.
        self.last_status: dict[str, Any] | None = None
        #: Set as the app closes, so the poller's wait ends at once rather than after a poll.
        self._poll_stop = threading.Event()

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
        self._poll_stop.set()
        if self._log_handler is not None:
            logging.getLogger("flux_orchestrator").removeHandler(self._log_handler)

    # -- the session --

    def edit_session(self) -> None:
        s = self.session
        fields = [
            FormField("unit", "unit host", s.unit, "mast03"),
            FormField("unit_port", "unit port", str(s.unit_port)),
            FormField("flux_meter", "flux-meter URL (e.g. http://mast-ns-spec:8003)", s.flux_meter, "not set"),
            FormField("output_root", "output root", s.output_root),
            FormField(
                "ra", "RA J2000, decimal hours (empty: none)", "" if s.ra_j2000_hours is None else str(s.ra_j2000_hours)
            ),
            FormField("dec", "Dec J2000, decimal degrees", "" if s.dec_j2000_degs is None else str(s.dec_j2000_degs)),
            FormField("object", "or an object name the unit resolves", s.object_name or ""),
        ]
        self.push_screen(
            Form("Session", fields, "enter or ctrl+s: save · esc: back", check=lambda v: _why(self._session_values(v))),
            self._session_edited,
        )

    def _session_values(self, values: dict[str, str]) -> Session | str:
        """The session these form values make, or why they cannot be taken."""
        try:
            unit = values["unit"].strip()
            if any(c in unit for c in ":/ "):
                return f"unit host '{unit}' should be a bare name like mast03; the port has its own field"
            port = int(values["unit_port"])
            if not 1 <= port <= 65535:
                return f"unit port {port} is not 1 to 65535"
            ra = float(values["ra"]) if values["ra"].strip() else None
            dec = float(values["dec"]) if values["dec"].strip() else None
        except ValueError as ex:
            return f"not a number: {ex}"
        if ra is not None and not (math.isfinite(ra) and 0 <= ra < 24):
            return f"RA {ra} is not 0 to 24 hours"
        if dec is not None and not (math.isfinite(dec) and -90 <= dec <= 90):
            return f"Dec {dec} is not -90 to +90 degrees"
        if (ra is None) != (dec is None):
            return "RA and Dec go together"
        flux = values["flux_meter"].strip()
        if flux and not flux.startswith(("http://", "https://")):
            return f"flux-meter URL '{flux}' should start with http://"
        candidate = copy.deepcopy(self.session)
        candidate.unit, candidate.unit_port, candidate.flux_meter = unit, port, flux
        candidate.output_root = values["output_root"].strip() or self.session.output_root
        candidate.ra_j2000_hours, candidate.dec_j2000_degs = ra, dec
        candidate.object_name = values["object"].strip() or None
        return candidate

    def _session_edited(self, values: dict[str, str] | None) -> None:
        if values is None:
            return
        made = self._session_values(values)
        if isinstance(made, str):
            return
        self.session.__dict__.update(made.__dict__)
        self.session.save(self.session_path)
        if self.main is not None:
            self.main.refresh_session()
        self.read_camera()

    # -- a run --

    def open_run(self, kind: Kind) -> None:
        if self.active is not None:
            self.notify("A run is going.", severity="warning", markup=False)
            return
        fields = [
            FormField(
                f.name,
                label_of(f.name) + ("  (changed)" if f.changed else ""),
                "" if f.value is None else _shown(f.value),
                f"default {f.default}",
            )
            for f in self.session.fields(kind)
        ]
        note = "enter or ctrl+s: check and start · esc: back"
        self.push_screen(
            Form(f"{TITLES[kind]}: parameters", fields, note, check=lambda v: self._run_values(kind, v)),
            lambda values: self._run_edited(kind, values),
        )

    def _focuser(self) -> dict[str, Any] | None:
        return (self.last_status or {}).get("focuser")

    def _run_values(self, kind: Kind, values: dict[str, str]) -> str | None:
        """Why these values cannot start a run, or None; checked on a copy, so nothing is kept."""
        candidate = copy.deepcopy(self.session)
        try:
            for name, text in values.items():
                candidate.set_param(kind, name, text)
        except SessionError as ex:
            return str(ex)
        return candidate.why_not_ready() or validate(kind, candidate.params(kind), self._focuser())

    def _run_edited(self, kind: Kind, values: dict[str, str] | None) -> None:
        if values is None:
            return
        for name, text in values.items():
            self.session.set_param(kind, name, text)
        self.session.last_run = kind.value
        self.session.save(self.session_path)
        text = summary(kind, self.session.params(kind), self.session, self._focuser())
        self.push_screen(Confirm(text + "\n\nStart it?"), lambda yes: self._start_confirmed(kind, yes))

    def _start_confirmed(self, kind: Kind, yes: bool | None) -> None:
        if yes:
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
        record = run.record if run is not None else None
        # The run started guiding and nothing on its side stopped it: a fiber-focus run that got that far.
        guiding_left = (
            getattr(record, "guide_settle", None) is not None
            and getattr(record, "stopped_by", None) != "the app (hard stop)"
            and not getattr(record, "unit_aborted", False)
        )
        self.post_message(RunFinished(kind, folder, terminal, error, guiding_left))

    def on_run_event_posted(self, message: RunEventPosted) -> None:
        if self.active is not None:
            self.active.folder = message.event.folder
        if self.run_screen is not None:
            self.run_screen.show_event(message.event)

    def on_log_line(self, message: LogLine) -> None:
        if self.run_screen is not None and self.active is not None:
            for log in self.run_screen.query("#run-log").results(RichLog):
                log.write(Text(message.line))

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
            self.push_screen(
                Confirm(
                    "Stop the run? It stops at its next step and cleans up: a fiber-focus run sends the "
                    "focuser back to its known-good position and leaves guiding running; a spiral is "
                    "closed. Safe for the telescope."
                ),
                self._stop_confirmed,
            )
        else:
            self.push_screen(
                Confirm(
                    "Hard stop: send unit/abort now? For a run that does not stop by itself. It stops "
                    "everything on the unit, guiding and tracking included."
                ),
                self._hard_stop_confirmed,
            )

    def _stop_confirmed(self, yes: bool | None) -> None:
        if yes and self.active is not None:
            self.active.stop.request(StopKind.STOP)
            self.notify("Stopping at the next step. Press s again for a hard stop.", markup=False)

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
                    self.post_message(UnitStatus(describe_unit(status), ok=True, status=status))
                except Exception as ex:  # noqa: BLE001 -- shown on the panel; a poller that raised would end the app, run and all
                    self.post_message(UnitStatus(f"not reachable: {type(ex).__name__}: {ex}", ok=False))
            # In short waits that watch the worker's cancellation, which the app sets as it shuts
            # down: one long sleep would hold the exit for a whole poll.
            deadline = time.monotonic() + self.poll_s
            while time.monotonic() < deadline:
                if worker.is_cancelled or self._poll_stop.wait(POLL_CHECK_S):
                    return

    def on_unit_status(self, message: UnitStatus) -> None:
        if message.status is not None:
            self.last_status = message.status
        title = Text.assemble((f"Unit {self.session.unit}", "bold"), ("" if message.ok else "  not reachable", "red"))
        self._update_panel("#unit-panel", Text.assemble(title, "\n", message.text))

    def _update_panel(self, selector: str, text: Text) -> None:
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
            text = Text.assemble(
                ("Flux camera", "bold"), f"\n{info.model}  sn {info.serial_number}  full scale {info.saturation_level}"
            )
        except Exception as ex:  # noqa: BLE001 -- shown on the panel, whatever the service did
            text = Text.assemble(("Flux camera", "bold"), ("  not reachable", "red"), f"\n{ex}")
        self.call_from_thread(self._show_camera, text)

    def _show_camera(self, text: Text) -> None:
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
            self.notify("A run is going: stop it first (s on its page).", severity="warning", markup=False)
            return
        if "Guiding" in ((self.last_status or {}).get("activities_verbal") or []):
            self.push_screen(
                Confirm(
                    "The unit is still guiding. Quitting leaves it guiding; stop it through the unit's API. Quit anyway?"
                ),
                self._quit_confirmed,
            )
            return
        self.exit()

    def _quit_confirmed(self, yes: bool | None) -> None:
        if yes:
            self.exit()

    async def action_quit(self) -> None:
        """Textual's own quit, which Ctrl+Q and the command palette reach: refused mid-run too."""
        self.action_quit_app()


def _why(made: Session | str) -> str | None:
    return made if isinstance(made, str) else None


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
