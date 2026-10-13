"""The three endpoints the unit's `RemoteFluxMeter` calls, and one for operators that saves a frame.

The camera is opened when the app starts and closed when it stops; there is no open, claim
or token on the wire. FastAPI runs these sync endpoints in a threadpool, so one lock
serializes every camera call: the SDK is not documented as thread-safe, and a configure
landing between another request's arm and trigger would change the frame under it. Two
runs at once would still interleave their calls -- that is ruled out by operating practice,
not here.
"""

from __future__ import annotations

import datetime
import io
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Response

from flux_meter_service.camera import Camera, FluxMeterError
from flux_meter_service.models import CameraInfo, SavedFrame, Settings
from flux_meter_service.save import DEFAULT_SAVE_ROOT, Exposure, save_frame

NPY_MEDIA_TYPE = "application/octet-stream"


def create_app(camera: Camera, save_root: Path = DEFAULT_SAVE_ROOT) -> FastAPI:
    """The service around `camera`, which it opens at startup and holds until shutdown.

    A camera that will not open raises out of startup, so the server stops instead of
    answering every call with an error. `save_root` is where `/expose_and_save` files frames.
    """
    lock = threading.Lock()
    # Numbering and writing a frame is serialized apart from the camera, so a slow share
    # never holds up the units' `/expose`.
    save_lock = threading.Lock()
    # The camera reports no settings of its own; these are the last ones set through this
    # service, None until `/configure` succeeds once.
    configured: list[Settings | None] = [None]

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        camera.open()
        try:
            yield
        finally:
            camera.close()

    app = FastAPI(title="flux-meter", lifespan=lifespan)

    @app.get("/info")
    def info() -> CameraInfo:
        with lock:
            return CameraInfo(
                model=camera.model,
                serial_number=camera.serial_number,
                saturation_level=camera.saturation_level,
                description=camera.description,
            )

    @app.put("/configure")
    def configure(settings: Settings) -> None:
        with lock:
            try:
                camera.configure(settings.exposure_us, settings.gain, settings.black_level)
            except FluxMeterError as ex:
                raise HTTPException(HTTPStatus.BAD_REQUEST, detail=str(ex)) from ex
            configured[0] = settings

    @app.post("/expose")
    def expose() -> Response:
        with lock:
            try:
                frame = camera.expose()
            except FluxMeterError as ex:
                raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, detail=str(ex)) from ex
        buffer = io.BytesIO()
        np.save(buffer, frame, allow_pickle=False)
        return Response(content=buffer.getvalue(), media_type=NPY_MEDIA_TYPE)

    @app.post("/expose_and_save")
    def expose_and_save() -> SavedFrame:
        """Take one frame with the current settings and file it under the share as FITS."""
        with lock:
            started = datetime.datetime.now(datetime.UTC)
            try:
                frame = camera.expose()
            except FluxMeterError as ex:
                raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, detail=str(ex)) from ex
            exposure = Exposure(
                started=started,
                ended=datetime.datetime.now(datetime.UTC),
                settings=configured[0],
                model=camera.model,
                serial_number=camera.serial_number,
                saturation_level=camera.saturation_level,
            )
        with save_lock:
            try:
                path, sequence = save_frame(save_root, frame, exposure)
            except OSError as ex:
                raise HTTPException(
                    HTTPStatus.SERVICE_UNAVAILABLE, detail=f"could not save the frame under '{save_root}': {ex}"
                ) from ex
        return SavedFrame(path=str(path), sequence=sequence)

    return app
