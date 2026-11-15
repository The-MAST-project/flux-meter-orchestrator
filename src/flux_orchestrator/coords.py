"""Sky coordinates as an operator pastes them: sexagesimal, lettered, or a decimal pair."""

from __future__ import annotations

import math

from astropy import units as u
from astropy.coordinates import Angle

#: A decimal RA above this is degrees, as catalogs give it; at or below it, hours.
HOURS_IN_A_DAY = 24.0
DEGREES_PER_HOUR = 15.0
#: `01 58 01.2 +28 31 12`: hours, minutes, seconds, then degrees, minutes, seconds.
SPACED_TOKENS = 6


def parse_coordinates(text: str) -> tuple[float, float]:
    """(RA J2000 decimal hours, Dec J2000 decimal degrees) from pasted text.

    Accepted: `01:58:01.2 +28:31:12`, `01 58 01.2 +28 31 12`, `01h58m01.2s +28d31m12s`, and a
    decimal pair `1.967 28.52`, whose RA is hours unless it is over 24, then degrees. A comma
    between the two is fine. Raises ValueError, naming what could not be read.
    """
    cleaned = text.strip().replace(",", " ")
    if not cleaned:
        raise ValueError("nothing to read")
    parts = cleaned.split()
    if len(parts) == 2 and all(_is_number(p) for p in parts):
        ra, dec = float(parts[0]), float(parts[1])
        if ra > HOURS_IN_A_DAY:
            ra /= DEGREES_PER_HOUR
    else:
        ra_text, dec_text = _halves(parts, text)
        try:
            ra = float(Angle(ra_text, u.hourangle).hour)
            dec = float(Angle(dec_text, u.deg).degree)
        except (ValueError, u.UnitsError) as ex:
            raise ValueError(f"'{text.strip()}' is not RA and Dec: {ex}") from ex
    if not (math.isfinite(ra) and 0 <= ra < HOURS_IN_A_DAY):
        raise ValueError(f"RA {ra:g} h is not 0 to 24 hours")
    if not (math.isfinite(dec) and -90 <= dec <= 90):
        raise ValueError(f"Dec {dec:g}° is not -90 to +90 degrees")
    return ra, dec


def sexagesimal(ra_hours: float, dec_degs: float) -> str:
    """`01:58:01.20 +28:31:12.0`, to check a parsed value by eye."""
    ra = Angle(ra_hours, u.hourangle).to_string(sep=":", precision=2, pad=True)
    dec = Angle(dec_degs, u.deg).to_string(sep=":", precision=1, alwayssign=True, pad=True)
    return f"{ra} {dec}"


def _halves(parts: list[str], text: str) -> tuple[str, str]:
    """RA and Dec as two strings: two tokens (`01:58:01.2 +28:31:12`, lettered), or six (spaces)."""
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == SPACED_TOKENS:
        return " ".join(parts[:3]), " ".join(parts[3:])
    raise ValueError(f"'{text.strip()}' is not RA and Dec: expected two values, or six with spaces")


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True
