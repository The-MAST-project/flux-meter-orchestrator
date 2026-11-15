"""`python -m flux_meter_service --port N [--host H] [--fake] [--dll-dir DIR] [--save-root DIR]`.

The port has no default: the units hard-code it as `FLUX_METER_PORT` (8003, in MAST_unit
`src/flux_metering/remote.py`), and a default here would be a second place to keep in step
with it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from flux_meter_service.app import create_app
from flux_meter_service.camera import Camera
from flux_meter_service.fake import FakeCamera
from flux_meter_service.save import DEFAULT_SAVE_ROOT
from flux_meter_service.thorcam import ThorCam

DEFAULT_HOST = "0.0.0.0"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="flux_meter_service", description="Serve the Zelux flux camera over HTTP.")
    parser.add_argument("--port", type=int, required=True, help="must match the units' FLUX_METER_PORT (8003)")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"interface to listen on (default {DEFAULT_HOST})")
    parser.add_argument("--fake", action="store_true", help="serve noise frames instead of the camera, for network tests")
    parser.add_argument(
        "--dll-dir", type=Path, help="directory holding the Thorlabs SDK's native DLLs, if they are not on PATH (Windows)"
    )
    parser.add_argument(
        "--save-root",
        type=Path,
        default=DEFAULT_SAVE_ROOT,
        help=f"where /expose_and_save files frames (default {DEFAULT_SAVE_ROOT})",
    )
    args = parser.parse_args(argv)
    if args.dll_dir is not None and sys.platform != "win32":
        parser.error("--dll-dir is Windows-only: it calls os.add_dll_directory")
    if args.dll_dir is not None and not args.dll_dir.is_dir():
        parser.error(
            f"--dll-dir {args.dll_dir} does not exist. Leave it out when the SDK package carries its own DLLs "
            "(thorlabs_tsi_sdk\\lib in the venv, as on mast-ns-spec)."
        )
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)

    if args.dll_dir is not None and sys.platform == "win32":
        # Held for the process lifetime: closing the handle removes the directory again.
        _dll_directory = os.add_dll_directory(str(args.dll_dir.resolve()))

    camera: Camera = FakeCamera() if args.fake else ThorCam()
    # The camera opens in the lifespan, so a server without lifespan support must not start.
    uvicorn.run(create_app(camera, save_root=args.save_root), host=args.host, port=args.port, lifespan="on")


if __name__ == "__main__":
    main()
