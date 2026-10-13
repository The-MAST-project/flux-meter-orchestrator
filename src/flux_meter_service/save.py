"""A flux frame written as FITS under the share, numbered within its observing night.

The layout is `<root>/<observing night>/flux-meter/flux-NNNN.fits`, beside the spectrographs'
`highspec` / `deepspec` folders under the share's `mast-ns-spec` tree. The night is the UTC date
twelve hours earlier, as MAST_common's `observing_night` defines it, so a night's frames share
one folder with the units' products of the same night.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits

from flux_meter_service.models import Settings

#: Where the share's tree for this host lives. A UNC path, not a drive letter: a mapped drive
#: belongs to one logon session and is invisible to a process started from another.
DEFAULT_SAVE_ROOT = Path(r"\\mast-ns-control\mast-share\MAST\mast-ns-spec")
FRAME_FOLDER = "flux-meter"
FRAME_PREFIX = "flux-"
SEQUENCE_DIGITS = 4
NIGHT_ROLLOVER = datetime.timedelta(hours=12)
US_PER_S = 1_000_000
ORIGIN = "flux-meter service"

_FRAME_PATTERN = re.compile(rf"^{re.escape(FRAME_PREFIX)}(\d+)\.fits$")


@dataclass(frozen=True)
class Exposure:
    """What is known about one frame besides its pixels."""

    started: datetime.datetime
    ended: datetime.datetime
    settings: Settings | None
    model: str
    serial_number: str
    saturation_level: int


def observing_night(when: datetime.datetime) -> str:
    return f"{(when - NIGHT_ROLLOVER).date():%Y-%m-%d}"


def frame_name(sequence: int) -> str:
    return f"{FRAME_PREFIX}{sequence:0{SEQUENCE_DIGITS}d}.fits"


def next_sequence(folder: Path) -> int:
    """One past the highest number already in `folder`, so a restart never reuses one."""
    numbers = [int(m.group(1)) for p in folder.iterdir() if (m := _FRAME_PATTERN.match(p.name))]
    return max(numbers, default=0) + 1


def _fits_time(when: datetime.datetime) -> str:
    """ISO 8601 in UTC with milliseconds and no offset, the FITS form for DATE-OBS."""
    return when.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _header(sequence: int, exposure: Exposure) -> fits.Header:
    header = fits.Header()
    header["SEQNUM"] = (sequence, "frame number within the night")
    header["DATE-OBS"] = (_fits_time(exposure.started), "UTC, exposure requested")
    header["DATE-END"] = (_fits_time(exposure.ended), "UTC, frame received")
    if exposure.settings is not None:
        header["EXPTIME"] = (exposure.settings.exposure_us / US_PER_S, "[s] last set via /configure")
        header["GAIN"] = (exposure.settings.gain, "last set via /configure")
        header["BLKLEVEL"] = (exposure.settings.black_level, "black level, last set via /configure")
    header["SATLEVEL"] = (exposure.saturation_level, "[counts] full scale")
    header["CAMMODEL"] = exposure.model
    header["CAMSERNO"] = (exposure.serial_number, "camera serial number")
    header["ORIGIN"] = ORIGIN
    return header


def save_frame(root: Path, frame: np.ndarray, exposure: Exposure) -> tuple[Path, int]:
    """Write `frame` as the night's next numbered FITS file. Returns its path and number.

    Written under a hidden name and renamed into place, so nobody reading the share sees a
    half-written file under a frame's name.
    """
    folder = root / observing_night(exposure.started) / FRAME_FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    sequence = next_sequence(folder)
    path = folder / frame_name(sequence)
    partial = folder / f".{path.name}.part"
    fits.PrimaryHDU(data=frame, header=_header(sequence, exposure)).writeto(partial)
    partial.rename(path)
    return path, sequence
