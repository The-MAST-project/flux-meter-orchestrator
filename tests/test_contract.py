"""The unit's `RemoteFluxMeter` client against the service run for real with `--fake`.

The client cannot be imported here: `flux_metering.remote` imports MAST_common
(`common.api`, `common.config`, `common.mast_logging`), and this service depends on none
of MAST. So its requests are reproduced exactly -- method, path, JSON body, a client built
with `trust_env=False` -- and its replies are parsed the way it parses them: `/info`
validated into the same four fields, a refusal's reason read from `detail`, and a frame
loaded with `np.load(..., allow_pickle=False)` and required to be 2-D.

Source: MAST_unit `src/flux_metering/remote.py`, branch `eli/remote-flux-meter`.
"""

from __future__ import annotations

import io

import httpx
import numpy as np
from pydantic import BaseModel

CALL_TIMEOUT_S = 10.0
EXPOSE_SLACK_S = 10.0
MICROSECONDS_PER_SECOND = 1_000_000
FRAME_NDIM = 2
EXPOSURE_US = 1000


class FluxMeterInfo(BaseModel):
    model: str
    serial_number: str
    saturation_level: int
    description: str


def _call(base_url: str, method: str, path: str, **kwargs) -> httpx.Response:
    kwargs.setdefault("timeout", CALL_TIMEOUT_S)
    with httpx.Client(trust_env=False) as client:
        return client.request(method, f"{base_url}{path}", **kwargs)


def test_the_client_contract_holds_against_the_fake_service(fake_service):
    base_url = f"http://{fake_service}"

    info_response = _call(base_url, "GET", "/info")
    assert info_response.is_success
    info = FluxMeterInfo.model_validate_json(info_response.content)
    assert "fake" in info.description.lower()

    refused = _call(base_url, "PUT", "/configure", json={"exposure_us": 0, "gain": 0, "black_level": 0})
    assert not refused.is_success
    assert "exposure" in str(refused.json()["detail"])

    configured = _call(base_url, "PUT", "/configure", json={"exposure_us": EXPOSURE_US, "gain": 0, "black_level": 0})
    assert configured.is_success

    read_timeout_s = EXPOSURE_US / MICROSECONDS_PER_SECOND + EXPOSE_SLACK_S
    exposed = _call(base_url, "POST", "/expose", timeout=httpx.Timeout(CALL_TIMEOUT_S, read=read_timeout_s))
    assert exposed.is_success
    frame = np.load(io.BytesIO(exposed.content), allow_pickle=False)
    assert frame.ndim == FRAME_NDIM
    assert frame.max() <= info.saturation_level
