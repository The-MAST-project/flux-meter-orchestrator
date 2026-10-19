"""The flux-meter service's camera, over its HTTP contract (see the repository README)."""

from __future__ import annotations

import datetime
import io
from dataclasses import dataclass

import httpx
import numpy as np

from flux_meter_service.models import CameraInfo, Settings

REQUEST_TIMEOUT_S = 20.0
#: The service waits the exposure plus 5 s for a frame (`thorcam.POLL_SLACK_MS`); this must stay above it.
EXPOSE_SLACK_S = 10.0
US_PER_S = 1_000_000
ERROR_BODY_CHARS = 400


class FluxMeterApiError(RuntimeError):
    """The service refused a call, failed it, or could not be reached."""


@dataclass(frozen=True)
class FluxFrame:
    data: np.ndarray
    #: On this machine's clock: when the request went out and when the frame came back. The
    #: exposure lies inside.
    requested: datetime.datetime
    received: datetime.datetime


class FluxMeterClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=REQUEST_TIMEOUT_S, trust_env=False)
        self._exposure_s = 0.0

    def info(self) -> CameraInfo:
        return CameraInfo.model_validate(self._checked(self._client.get(f"{self.base_url}/info")).json())

    def configure(self, settings: Settings) -> None:
        self._checked(self._client.put(f"{self.base_url}/configure", json=settings.model_dump()))
        self._exposure_s = settings.exposure_us / US_PER_S

    def expose(self) -> FluxFrame:
        requested = datetime.datetime.now(datetime.UTC)
        response = self._checked(self._client.post(f"{self.base_url}/expose", timeout=self._exposure_s + EXPOSE_SLACK_S))
        received = datetime.datetime.now(datetime.UTC)
        return FluxFrame(
            data=np.load(io.BytesIO(response.content), allow_pickle=False), requested=requested, received=received
        )

    @staticmethod
    def _checked(response: httpx.Response) -> httpx.Response:
        if response.is_error:
            raise FluxMeterApiError(
                f"{response.request.method} {response.request.url}: HTTP {response.status_code}: "
                f"{response.text[:ERROR_BODY_CHARS]}"
            )
        return response
