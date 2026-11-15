"""Coordinates as an operator pastes them."""

from __future__ import annotations

import pytest

from flux_orchestrator.coords import parse_coordinates, sexagesimal

RA_H = 1 + 58 / 60 + 1.2 / 3600
DEC_D = 28 + 31 / 60 + 12 / 3600


@pytest.mark.parametrize(
    "text",
    [
        "01:58:01.2 +28:31:12",
        "01 58 01.2 +28 31 12",
        "01h58m01.2s +28d31m12s",
        "  01:58:01.2, +28:31:12 ",
        f"{RA_H * 15:.6f} {DEC_D:.6f}",
        f"{RA_H:.6f} {DEC_D:.6f}",
    ],
)
def test_the_usual_forms_read_the_same(text):
    ra, dec = parse_coordinates(text)
    assert ra == pytest.approx(RA_H, abs=1e-5)
    assert dec == pytest.approx(DEC_D, abs=1e-4)


def test_a_southern_dec_keeps_its_sign():
    assert parse_coordinates("05:35:17.3 -05:23:28")[1] == pytest.approx(-(5 + 23 / 60 + 28 / 3600), abs=1e-4)


@pytest.mark.parametrize("text", ["", "vega", "1.0", "25:00:00 +10:00:00", "01:00:00 +95:00:00", "nan 10"])
def test_what_is_not_a_position_is_refused(text):
    with pytest.raises(ValueError):
        parse_coordinates(text)


def test_the_check_form_reads_back():
    assert sexagesimal(RA_H, DEC_D) == "01:58:01.20 +28:31:12.0"
