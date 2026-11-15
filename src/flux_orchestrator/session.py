"""The app's session: which unit and services, the target, and each run's parameters, kept in a JSON
file between sessions so the next one opens where the last left off.

Each run's parameters are its own frozen dataclass (`RunParams`, `SpiralsParams`, `FiberFocusParams`);
the session keeps only the values changed from their defaults, and builds the dataclass from them, so
a default changed in a run reaches every session that never overrode it.
"""

from __future__ import annotations

import dataclasses
import json
import math
import types
import typing
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from flux_orchestrator.fiber_focus import FiberFocusParams
from flux_orchestrator.run import DEFAULT_OUTPUT_ROOT, RunParams
from flux_orchestrator.save_spirals import SpiralsParams
from flux_orchestrator.unit_client import UNIT_PORT, Target

SESSION_FILE = "flux-orchestrator-session.json"
#: Set by the session itself, not edited per run.
SESSION_OWNED = frozenset({"unit", "target"})


class Kind(StrEnum):
    SAVE_SPIRALS = "save_spirals"
    FIBER_FOCUS = "fiber_focus"
    MAX_FLUX = "max_flux"


PARAMS: dict[Kind, type] = {
    Kind.SAVE_SPIRALS: SpiralsParams,
    Kind.FIBER_FOCUS: FiberFocusParams,
    Kind.MAX_FLUX: RunParams,
}
TITLES: dict[Kind, str] = {
    Kind.SAVE_SPIRALS: "Save spirals",
    Kind.FIBER_FOCUS: "Fiber focus",
    Kind.MAX_FLUX: "Max flux",
}


_NAMES = {int: "whole number", float: "number", str: "text"}


class SessionError(ValueError):
    """A value the session cannot take."""


@dataclass(frozen=True)
class EditableField:
    name: str
    #: The annotation as written, for the form's hint.
    type_name: str
    default: Any
    value: Any

    @property
    def changed(self) -> bool:
        return self.value != self.default


@dataclass
class Session:
    unit: str = ""
    unit_port: int = UNIT_PORT
    flux_meter: str = ""
    output_root: str = str(DEFAULT_OUTPUT_ROOT)
    ra_j2000_hours: float | None = None
    dec_j2000_degs: float | None = None
    object_name: str | None = None
    #: Per kind, the parameters changed from the run's defaults.
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The run set up last, which the main page offers first.
    last_run: str | None = None

    @property
    def target(self) -> Target:
        return Target(ra_j2000_hours=self.ra_j2000_hours, dec_j2000_degs=self.dec_j2000_degs, object_name=self.object_name)

    def why_not_ready(self) -> str | None:
        """Why no run can start yet, or None."""
        if not self.unit:
            return "no unit"
        if not self.flux_meter:
            return "no flux-meter URL"
        if (self.ra_j2000_hours is None) != (self.dec_j2000_degs is None):
            return "RA and Dec go together"
        return None

    def params(self, kind: Kind) -> Any:
        """The run's parameters: its defaults, this session's unit and target, and the overrides."""
        fields = typing.get_type_hints(PARAMS[kind])
        values = {
            name: _from_json(kind, name, value)
            for name, value in self.overrides.get(kind, {}).items()
            if name in fields and name not in SESSION_OWNED
        }
        return PARAMS[kind](unit=self.unit, target=self.target, **values)

    def fields(self, kind: Kind) -> list[EditableField]:
        defaults = PARAMS[kind](unit="")
        current = self.params(kind)
        hints = typing.get_type_hints(PARAMS[kind])
        return [
            EditableField(f.name, _type_name(hints[f.name]), getattr(defaults, f.name), getattr(current, f.name))
            for f in dataclasses.fields(PARAMS[kind])
            if f.name not in SESSION_OWNED
        ]

    def set_param(self, kind: Kind, name: str, text: str) -> None:
        """Parse `text` as the field's type and keep it; an empty text goes back to the default."""
        hints = typing.get_type_hints(PARAMS[kind])
        if name not in hints or name in SESSION_OWNED:
            raise SessionError(f"{TITLES[kind]} has no parameter {name}")
        value = parse(hints[name], text)
        default = getattr(PARAMS[kind](unit=""), name)
        kept = self.overrides.setdefault(kind, {})
        if value == default:
            kept.pop(name, None)
        else:
            kept[name] = _to_json(value)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Session:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses.fields(cls)}
        session = cls(**{name: value for name, value in raw.items() if name in known})
        # A parameter a run no longer has (renamed, removed) is dropped, not passed to it.
        for kind in list(session.overrides):
            if kind not in {k.value for k in Kind}:
                del session.overrides[kind]
                continue
            fields = typing.get_type_hints(PARAMS[Kind(kind)])
            session.overrides[kind] = {
                n: v for n, v in session.overrides[kind].items() if n in fields and n not in SESSION_OWNED
            }
        return session


def parse(annotation: Any, text: str) -> Any:
    """`text` as a value of `annotation`: int, float, bool, str, a tuple of ints, or any of those
    or None. An empty text is None where None is allowed."""
    text = text.strip()
    args = typing.get_args(annotation)
    optional = _is_union(annotation) and type(None) in args
    if optional:
        if not text:
            return None
        annotation = next(a for a in args if a is not type(None))
    if annotation is bool:
        if text.lower() in {"true", "yes", "1", "on"}:
            return True
        if text.lower() in {"false", "no", "0", "off"}:
            return False
        raise SessionError(f"'{text}' is not true or false")
    if typing.get_origin(annotation) is tuple:
        return _parse_ints(text, len(typing.get_args(annotation)))
    if annotation in (int, float, str):
        try:
            value = annotation(text)
        except ValueError as ex:
            raise SessionError(f"'{text}' is not a {_NAMES.get(annotation, annotation.__name__)}") from ex
        if isinstance(value, float) and not math.isfinite(value):
            raise SessionError(f"'{text}' is not a finite number")
        return value
    raise SessionError(f"cannot edit a {annotation}")


def _parse_ints(text: str, want: int) -> tuple[int, ...]:
    """Exactly `want` whole numbers, separated by spaces or commas."""
    try:
        values = tuple(int(part) for part in text.replace(",", " ").split())
    except ValueError as ex:
        raise SessionError(f"'{text}' is not whole numbers") from ex
    if len(values) != want:
        raise SessionError(f"'{text}' is not {want} whole numbers")
    return values


def _is_union(annotation: Any) -> bool:
    return typing.get_origin(annotation) in (typing.Union, types.UnionType)


def _type_name(annotation: Any) -> str:
    if _is_union(annotation):
        return " | ".join(_type_name(a) for a in typing.get_args(annotation))
    if annotation is type(None):
        return "None"
    if typing.get_origin(annotation) is tuple:
        return "x y"
    return getattr(annotation, "__name__", str(annotation))


def _to_json(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _from_json(kind: Kind, name: str, value: Any) -> Any:
    annotation = typing.get_type_hints(PARAMS[kind]).get(name)
    if isinstance(value, list) and annotation is not None:
        return tuple(value)
    return value
