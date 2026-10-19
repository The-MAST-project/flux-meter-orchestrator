# flux-meter

HTTP service that holds the Thorlabs Zelux flux camera on `mast-ns-spec` and serves it to MAST units, and `flux_orchestrator`, which runs flux metering from mast-ns-spec against one unit. See `README.md` and `DECISIONS.md`.

## Commands

- Tests: `.venv/bin/python -m pytest`
- Lint and format: `.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .`
- Types: `npx pyright`
- Dependencies are managed with uv (`uv.lock`); the venv is `.venv/`, Python 3.12.

## Contract

| Call | Request | Reply |
|---|---|---|
| `GET /info` | — | JSON `model`, `serial_number`, `saturation_level`, `description` |
| `PUT /configure` | JSON `exposure_us`, `gain`, `black_level` (ints) | 200; refused → 400 `{"detail": ...}` |
| `POST /expose` | — | `.npy` bytes, `application/octet-stream`; failure → 503 `{"detail": ...}` |
| `POST /expose_and_save` | — | JSON `path`, `sequence`; FITS under `--save-root`; camera or share failure → 503 `{"detail": ...}`. For operators; the units do not call it |

The other half is `RemoteFluxMeter` in MAST_unit `src/flux_metering/remote.py`. A change here is a change to that client, and the reverse. `tests/test_contract.py` reproduces the client's requests (it cannot import the client, which needs MAST_common).

## Rules

- No dependency on MAST_common, MAST_unit or MAST_spec.
- The orchestrator drives a unit through its public HTTP API only (MAST_common `docs/scripting-the-api.md`), and never calls `expose` inside a spiral session. A missing capability is fixed in MAST_unit, not worked around here.
- `aperture_photometry_single.py` and `frame_shift.py` are copies of MAST_unit's; keep them comparable until the unit's go.
- Saved-frame paths (`<save-root>/<night>/flux-meter/flux-NNNN.fits`) are read by people and scripts; change them only deliberately.
- `--port` stays required: the port is the units' `FLUX_METER_PORT` (8003, in MAST_unit `src/flux_metering/remote.py`).
- The camera opens at startup and a failure exits the process; do not add reconnects, claims or open/close endpoints.
- The frame-poll slack (`thorcam.POLL_SLACK_MS`) must stay below the client's `EXPOSE_SLACK_S` (10 s).
- The Thorlabs SDK is not on PyPI and is not a declared dependency.
- Hosted at `elibrody-weizmann/flux-meter` (private), not The-MAST-project, until it moves somewhere durable. Push there.
