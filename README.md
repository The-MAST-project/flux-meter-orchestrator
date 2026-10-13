# flux-meter

A small HTTP service that serves the Thorlabs Zelux (CS165MU) flux camera to the MAST telescope units over the site network. It runs on `mast-ns-spec`, where the camera is attached by USB; the units reach it with `RemoteFluxMeter` (MAST_unit `src/flux_metering/remote.py`), which is the other half of this contract.

The service opens the camera when it starts and holds it until it stops. There is no open, close, claim or token on the wire: one flux-metering run at a time is an operating rule, and the service only serializes individual camera calls with a lock.

## Contract

| Call | Request | Reply |
|---|---|---|
| `GET /info` | — | JSON: `model`, `serial_number`, `saturation_level`, `description` |
| `PUT /configure` | JSON `exposure_us`, `gain`, `black_level` (integers) | 200. Settings the camera refuses: 400 with `{"detail": "<the camera's reason>"}` |
| `POST /expose` | — | One frame as `.npy` bytes (`np.save`), `application/octet-stream`. A camera failure: 503 with `{"detail": "<reason>"}` |
| `POST /expose_and_save` | — | For operators, not the units. One frame with the current settings, written as FITS to `<save-root>/<observing night>/flux-meter/flux-NNNN.fits`; returns `{"path", "sequence"}`. A camera failure, or a share it cannot write: 503 with `{"detail"}` |

The client's `/expose` read timeout is the exposure plus 10 s. The service waits the exposure plus 5 s (`thorcam.POLL_SLACK_MS`) for a triggered frame, which must stay below that.

## Running

```
python -m flux_meter_service --port PORT [--host HOST] [--fake] [--dll-dir DIR] [--save-root DIR]
```

- `--port` is required and has no default. It **must match the units' `FLUX_METER_PORT` (8003, in MAST_unit `src/flux_metering/remote.py`)**, where the units hard-code it for this stopgap.
- `--host` defaults to `0.0.0.0`.
- `--fake` serves noise frames shaped like the Zelux's (1440x1080, 10-bit, full scale 1023) instead of opening the camera. Its `model` and `description` say it is fake. Use it to test the network path from a unit in daylight, or before the camera is installed.
- `--save-root` is where `/expose_and_save` files frames. Default `\\mast-ns-control\mast-share\MAST\mast-ns-spec`, beside the spectrographs' `highspec` / `deepspec` folders. Frames are numbered from 1 within each observing night (the UTC date twelve hours earlier), continuing from the highest number already there, so a restart never reuses one. The header carries the sequence number, request and arrival times, the camera model, serial number and full scale, and the exposure, gain and black level last set through `/configure` (left out if none was set since the service started).
- `--dll-dir` (Windows only) adds a directory to the DLL search path with `os.add_dll_directory` before the SDK loads, so the Thorlabs native DLLs need not be on `PATH`.

If the real camera does not open at startup, the process logs the reason and exits (uvicorn's startup-failure code, 3). It does not stay up answering errors. After the camera is unplugged or the service fails, restart it; there is no reconnect logic.

## Install on mast-ns-spec (Windows 10)

1. **Python 3.12 and uv.** Install Python 3.12 and [uv](https://docs.astral.sh/uv/).
2. **ThorCam / ThorImageCAM.** Install Thorlabs' camera software. It provides the USB kernel driver, which the SDK's Python package does not. A working install shows **`Thorlabs Camera Zelux`, status OK** in Device Manager. A bare `TSI` device with problem code 28 means the driver is missing.
3. **The service.** Copy this repository to the machine and, in it, run:

   ```
   uv sync --no-dev
   ```

4. **The Thorlabs SDK.** `thorlabs_tsi_sdk` is not on PyPI. It ships inside Thorlabs' *Scientific Camera Interfaces* SDK bundle (installed with ThorCam, typically as `Scientific Camera Interfaces.zip` under `C:\Program Files\Thorlabs\Scientific Imaging\Scientific Camera Support\`). Unpack it; the Python package is under `SDK\Python Toolkit\` and the 64-bit native DLLs under `SDK\Native Toolkit\dlls\`. The exact layout varies between releases, so check the unpacked bundle. Install the package into the service's venv:

   ```
   uv pip install "<path to the Python Toolkit package>"
   ```

   `uv sync` removes packages that are not in `uv.lock`, so repeat this after any `uv sync` (or use `uv sync --inexact`).
5. **The DLLs.** Either copy the 64-bit DLLs to a fixed directory and pass it as `--dll-dir`, or put that directory on `PATH`.
6. **Windows Firewall.** Allow the port inbound, from an elevated PowerShell:

   ```powershell
   New-NetFirewallRule -DisplayName "MAST flux-meter" -Direction Inbound -Protocol TCP -LocalPort <PORT> -Action Allow
   ```

## Running by hand

```
.venv\Scripts\python -m flux_meter_service --port <PORT> --dll-dir C:\Thorlabs\dlls
```

On success the log shows `opened CS165MU sn=... bit_depth=10 ...` and uvicorn listening. From a unit:

```
curl --noproxy * http://mast-ns-spec:<PORT>/info
```

For a daylight network test without the camera, add `--fake` and drop `--dll-dir`.

## Development

Built and tested on macOS; the Thorlabs SDK is not needed for the tests.

```
uv sync
.venv/bin/python -m pytest
.venv/bin/python -m ruff format . && .venv/bin/python -m ruff check .
npx pyright
```

## Structure

```
src/flux_meter_service/
  __main__.py   CLI, runs uvicorn
  app.py        create_app(camera, save_root): the endpoints and the locks
  camera.py     the Camera protocol and FluxMeterError
  models.py     request and response bodies
  save.py       /expose_and_save's FITS writer, folder layout and numbering
  thorcam.py    the Zelux through the Thorlabs SDK (copied from MAST_unit)
  fake.py       noise frames for --fake
tests/
  test_service.py   endpoints in process against a stub camera
  test_save.py      /expose_and_save: layout, numbering, header, failures
  test_cli.py       CLI parsing, exit when the camera will not open
  test_contract.py  the client's exact requests against the service under uvicorn --fake
```

## Dependencies

FastAPI, uvicorn, numpy and astropy (for the FITS writer), pinned in `uv.lock`. Tests use pytest and httpx. The Thorlabs SDK is installed separately (above). Nothing from MAST_common, MAST_unit or MAST_spec.
