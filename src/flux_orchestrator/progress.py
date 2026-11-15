"""What a run tells a watcher while it goes, and how a watcher asks it to stop.

The command-line runs need neither: Ctrl+C raises `KeyboardInterrupt` wherever the run is, as it
always has. The app runs a run on a worker thread, where no signal reaches it, so it passes a
`StopToken` instead. The run checks it in every wait and between frames and steps, and a requested
stop raises `StopRequested` once, a `KeyboardInterrupt`, so each run's existing Ctrl+C handling
applies unchanged. A run closes its token as it leaves its work, before any cleanup: the cleanup
(the focuser sent home, an abort confirmed) waits through the same calls, and a stop pressed while it
does must not cut it short.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)


class StopRequested(KeyboardInterrupt):
    """The app asked the run to stop; a run handles it as it handles Ctrl+C."""


class StopKind(StrEnum):
    #: Stop at the next check, with the run's own Ctrl+C handling.
    STOP = "stop"
    #: As STOP, after the app has also sent `unit/abort` to end a call that would not return.
    HARD_STOP = "hard stop"


class StopToken:
    """One run's stop request, shared by the app's thread and the run's."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._kind: StopKind | None = None
        self._raised = False
        self._closed = False

    def request(self, kind: StopKind = StopKind.STOP) -> None:
        with self._lock:
            if self._kind is not StopKind.HARD_STOP:
                self._kind = kind

    @property
    def kind(self) -> StopKind | None:
        with self._lock:
            return self._kind

    def check(self) -> None:
        """Raise `StopRequested` the first time it is called after a request, unless the run has
        closed the token; never again."""
        with self._lock:
            if self._kind is None or self._raised or self._closed:
                return
            self._raised = True
        raise StopRequested(f"stop requested ({self._kind})")

    def close(self) -> None:
        """The run has left its work: from here on nothing raises."""
        with self._lock:
            self._closed = True

    def ended_by(self) -> StopKind | None:
        """The stop that ended the run: one that was raised, or a hard stop, whose `unit/abort` ends
        the run whether or not a check ever raised it."""
        with self._lock:
            if self._kind is StopKind.HARD_STOP or self._raised:
                return self._kind
            return None


def check(token: StopToken | None) -> None:
    if token is not None:
        token.check()


def close(token: StopToken | None) -> None:
    if token is not None:
        token.close()


def stopped_by(token: StopToken | None) -> str | None:
    kind = token.ended_by() if token is not None else None
    return f"the app ({kind})" if kind is not None else None


class EventKind(StrEnum):
    PHASE = "phase"
    STEP = "step"
    FRAME = "frame"
    END = "end"


@dataclass(frozen=True)
class RunEvent:
    """A snapshot: nothing in it is shared with the run's own records."""

    kind: EventKind
    folder: str
    phase: str | None = None
    #: Steps or positions done, and how many in all (None when the run cannot know, as a walk).
    done: int | None = None
    total: int | None = None
    message: str = ""
    detail: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


class Reporter:
    """Sends a run's events to its watcher, if it has one. A watcher that fails is logged and
    ignored: the display must never change how a run ends."""

    def __init__(self, folder: str, on_event: Callable[[RunEvent], None] | None) -> None:
        self.folder = folder
        self._on_event = on_event
        self.phase: str | None = None

    def emit(
        self,
        kind: EventKind,
        message: str = "",
        *,
        done: int | None = None,
        total: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        if self._on_event is None:
            return
        event = RunEvent(
            kind=kind,
            folder=self.folder,
            phase=self.phase,
            done=done,
            total=total,
            message=message,
            detail=MappingProxyType(dict(detail or {})),
        )
        try:
            self._on_event(event)
        except Exception:  # noqa: BLE001 -- the watcher is a display; a fault there must not end the run
            logger.exception("the run's watcher failed on an event; the run goes on")

    def enter(self, phase: str, message: str = "") -> None:
        self.phase = phase
        self.emit(EventKind.PHASE, message or phase)
