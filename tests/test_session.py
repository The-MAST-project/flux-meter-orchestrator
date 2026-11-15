"""The app's session: parameters typed from the runs' own dataclasses, overrides only, saved and loaded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flux_orchestrator.fiber_focus import FiberFocusParams
from flux_orchestrator.session import Kind, Session, SessionError


def test_a_new_session_builds_each_run_from_its_defaults_and_the_session_target():
    session = Session(unit="mast03", flux_meter="http://x:8003", ra_j2000_hours=1.967, dec_j2000_degs=28.52)

    params = session.params(Kind.FIBER_FOCUS)

    assert isinstance(params, FiberFocusParams)
    assert params.unit == "mast03"
    assert params.target.ra_j2000_hours == 1.967
    assert params.count == FiberFocusParams(unit="").count


def test_a_parameter_is_parsed_as_its_type_and_only_a_change_is_kept():
    session = Session(unit="mast03")

    session.set_param(Kind.FIBER_FOCUS, "count", "9")
    session.set_param(Kind.FIBER_FOCUS, "flux_seconds", "7")
    session.set_param(Kind.FIBER_FOCUS, "start", "22200")

    assert session.overrides[Kind.FIBER_FOCUS] == {"count": 9, "flux_seconds": 7.0, "start": 22200}
    session.set_param(Kind.FIBER_FOCUS, "start", "")
    assert "start" not in session.overrides[Kind.FIBER_FOCUS]


@pytest.mark.parametrize(
    ("kind", "name", "text"),
    [
        (Kind.FIBER_FOCUS, "count", "nine"),
        (Kind.MAX_FLUX, "skip_acquisition", "maybe"),
        (Kind.FIBER_FOCUS, "unit", "x"),
        (Kind.MAX_FLUX, "center", "512"),
        (Kind.MAX_FLUX, "center", "512 384 7"),
    ],
)
def test_a_value_the_field_cannot_take_is_refused(kind, name, text):
    with pytest.raises(SessionError):
        Session(unit="mast03").set_param(kind, name, text)


def test_a_pair_parameter_round_trips_through_the_file(tmp_path: Path):
    path = tmp_path / "session.json"
    session = Session(unit="mast03", flux_meter="http://x:8003")
    session.set_param(Kind.MAX_FLUX, "center", "4401 2407")
    session.save(path)

    loaded = Session.load(path)

    assert loaded.params(Kind.MAX_FLUX).center == (4401, 2407)
    assert loaded.unit == "mast03"


def test_the_editable_fields_leave_out_what_the_session_owns_and_mark_changes():
    session = Session(unit="mast03")
    session.set_param(Kind.SAVE_SPIRALS, "rings", "1")

    fields = {f.name: f for f in session.fields(Kind.SAVE_SPIRALS)}

    assert "unit" not in fields and "target" not in fields
    assert fields["rings"].changed and fields["rings"].value == 1
    assert not fields["step_seconds"].changed


@pytest.mark.parametrize(
    ("session", "why"),
    [
        (Session(flux_meter="http://x"), "no unit"),
        (Session(unit="mast03"), "no flux-meter URL"),
        (Session(unit="mast03", flux_meter="http://x", ra_j2000_hours=1.0), "RA and Dec go together"),
        (Session(unit="mast03", flux_meter="http://x"), None),
    ],
)
def test_a_session_says_why_it_cannot_run_yet(session, why):
    assert session.why_not_ready() == why


def test_a_missing_file_is_a_new_session(tmp_path: Path):
    assert Session.load(tmp_path / "none.json") == Session()


def test_a_parameter_a_run_no_longer_has_is_dropped_on_load(tmp_path: Path):
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps({"unit": "mast03", "overrides": {"fiber_focus": {"count": 5, "renamed_away": 1}, "gone_kind": {"x": 1}}})
    )

    session = Session.load(path)

    assert session.overrides == {"fiber_focus": {"count": 5}}
    assert session.params(Kind.FIBER_FOCUS).count == 5
