# flux-meter

A small HTTP service that serves the Thorlabs Zelux (CS165MU) flux camera to the MAST telescope units over the site network. It runs on `mast-ns-spec`, where the camera is attached by USB; the units reach it with `RemoteFluxMeter` (MAST_unit `src/flux_metering/remote.py`), which is the other half of this contract.

The service opens the camera when it starts and holds it until it stops. There is no open, close, claim or token on the wire: one flux-metering run at a time is an operating rule, and the service only serializes individual camera calls with a lock.

The repository also holds `flux_orchestrator`, which runs flux metering from mast-ns-spec instead of from inside a unit: it drives one unit through the unit's public HTTP API and takes its flux frames from this service. See [Flux-metering orchestrator](#flux-metering-orchestrator).

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

## Flux-metering orchestrator

Finds the pointing at which most starlight reaches the science fiber, and measures how far that is, in imager pixels, from where acquisition put the star. One run, against one unit:

1. The unit acquires the target with no guider handover (`start_acquisition_and_guiding`, `handover_automatically_to_guider=false`); the run waits for `status.latest.acquisition` to leave `running` and goes on only on `ended`.
2. The folding mirror goes to SPEC (`stage/move_to_preset`), waited on by `stage.at_preset`.
3. The unit opens a spiral session that saves every step's frame (`spiral_new_path`, `save_intermediate_exposures=true`); it takes `reference.fits` and starts tracking.
4. At each cell, `--frames` flux frames from this service, each reduced by aperture photometry, then to their median. `spiral_next_step` moves to the next cell of PWI4's spiral and saves its imager frame. A complete ring with no improvement stops the walk (`--patience-rings`), as do `--max-rings` and `--max-radius`.
5. `spiral_end_path` closes the session, which stops tracking. The shift between `reference.fits` and the brightest cell's frame, both read from the share, is the answer, correlated around the fiber position the unit resolved for the session (read from the session's own `result.json`). When the brightest cell is the origin, whose frame is the reference itself, the shift is zero and nothing is correlated (`argmax_at_reference`).

A step for which PWI4 reports no cell is measured and recorded but completes no ring. A failure of the unit's own final frame in `spiral_end_path` is recorded and does not cost the run its result.

**Known gap.** The unit's in-built flux metering resets PWI4's spiral offset at the end of a run, returning the mount to the acquired pointing. The operator spiral does not, and the unit API has no call that does, so after a run the mount is left at the cell where the walk stopped (tracking off).

**What the unit must have.** `status.latest.acquisition` (MAST_unit#346, moved under `latest` by MAST_unit#371), the spiral session's record in `status.latest.spiral` and per-step timeout (MAST_unit#349, with MAST_common#212), and spiral steps taken as angles on the sky (MAST_unit#136), which `spiral_new_path` confirms by reporting the RA step it commanded. Against a unit without them the run fails at that step with the unit's own error. The spiral stays on the unit; this run never calls `expose`, which the unit refuses while a spiral session that takes frames is open (MAST_unit#385).

```
python -m flux_orchestrator --unit mast01 --flux-meter http://localhost:8003 --ra 20.68 --dec 43.55
python -m flux_orchestrator --unit mast01 --flux-meter http://localhost:8003 --object Vega
python -m flux_orchestrator --unit mast01 --flux-meter http://localhost:8003 --skip-acquisition   # shakedown, no measurement
```

Defaults follow the unit's own flux metering: 5 s exposures (imager and flux alike), 0.5" steps, 3 flux frames per step, 6 rings, 10" radius. `--center X Y` sets the fiber's imager position for the correlation; by default the unit resolves it from `guiding.rois[fcu_v2]`, then the frame center.

**Products.** `<output-root>/<observing night>/flux-orchestrator/NNNN/`: every flux frame as `flux-<step>-<n>.fits`, and `result.json` with the parameters, each step (cell, ring, flux, the imager frame's path on the share) and the result (`dx`, `dy`, confidence, the arg-max cell, the commanded offset in pixels to compare against). `--output-root` defaults to `\\mast-ns-control\mast-share\MAST\mast-ns-spec`. The unit's frames are read by the share paths the unit reports (`status.latest.spiral.shared_products_path`, each step's `image`), so the orchestrator runs on a Windows host on the share. `result.json` is written on every exit.

**Ctrl+C** aborts the unit while it acquires or moves the mirror, closes the spiral session (stopping tracking) once one has been requested, and leaves the unit alone once the session is closed. The exit status is 0 only for a converged run with a result.

The photometry and the correlation are copies of MAST_unit's (`aperture_photometry_single.py`, `frame_shift.py`), kept as they are there until the unit's flux metering is removed.

## Save-spirals run

Param's fixed-ring run (`acquire_and_save_spirals` in the unit's flux-metering branch), driven through the unit's public API. It measures nothing: it walks `--rings` rings of PWI4's spiral, `(2 × rings + 1)²` cells, and saves imager and flux frames at every cell for analysis afterwards.

1. Acquisition (`--acquisition-seconds`) and the folding mirror, as above.
2. The unit opens a spiral session that takes no frames (`spiral_new_path`, `frames=false`), confirmed by `status.latest.spiral.frames`.
3. At each cell, `expose` takes `--frames` imager frames (`--step-seconds`, full frame, bin 1) into a subfolder of its own, `save-spirals-<night>-<NNNN>-<ring>-<step>`. From the moment `expose` answers, flux frames (`--flux-seconds`) are taken back to back until `status.latest.exposure` says the series has ended. `spiral_next_step` then moves to the next cell. The walk ends when PWI4 reports a cell beyond the last ring, or after the last cell. A failed imager frame is recorded and the walk goes on; a series that saved no frame at all, or whose frames will not reach the share (the unit's share demoted, known from the first read), fails the run.
4. `spiral_end_path` closes the session, leaving tracking as it was found.

**Pairing.** Each imager frame's window is its `DATE-OBS` and exposure from `status.latest.exposure.frames`, in the unit's clock. The clock offset is measured at every cell: the unit stamps the record's `started` between receiving `expose` and answering it, so the offset lies within that round trip. The imager window, moved onto this host's clock, is paired with the flux frame that overlaps it most. A flux frame's window is the whole request, transfer included. An imager frame whose `DATE-OBS` predates the request is flagged (`predates_request`): PHD2, guiding, handed back the guide frame it already had.

**What the unit must have**, beyond the above: frameless spiral sessions (MAST_unit#385), the commanded RA step in `status.latest.spiral` (MAST_unit#391, with MAST_common#217), and per-frame records in `status.latest.exposure` (MAST_unit#327). A unit without them fails the run at the first point it shows, and is aborted.

```
python -m flux_orchestrator.save_spirals --unit mast01 --flux-meter http://localhost:8003 --ra 20.68 --dec 43.55 --rings 2 --frames 3
```

**Products.** `<output-root>/<observing night>/save-spirals/NNNN/`: the flux frames as `flux-<ring>-<step>-<n>.fits`, each header naming its cell, ring and the imager subfolder (`IMGDIR`), and `result.json`, rewritten after every cell, with the parameters and, per cell, the unit's exposure record, the share folder of its imager frames, the clock offset and the pairs. The imager frames stay where the unit files them, under the night's `Exposures` folder on the share.

**Ctrl+C or a failure** after the unit was first asked anything aborts it (`unit/abort`), which ends a series in flight and closes the session; `spiral_end_path` would refuse while one is in flight. A `spiral_end_path` that fails is a failure too. `unit/abort` answers before it has stopped anything, so the run then reads the unit's status until `latest.spiral` and `latest.exposure` have ended, for up to 30 s, and records whether they did (`unit_abort_confirmed`). The exit status is 0 only for a completed walk whose session closed.

## Fiber-focus run

Param's focus sweep (`acquire_and_focus_fiber` in the unit's flux-metering branch), driven through the unit's public API. It measures nothing: it guides on a star with the folding mirror in, steps the focuser through `--count` positions from `--start` (default: the unit's `known_as_good_position`) by `--step` (signed), and saves flux frames at every position for analysis afterwards.

1. **Refused up front**, before anything moves: the unit busy (`Acquiring`, `Guiding`, `Autofocusing`, `StartingUp`, `ShuttingDown`, `FluxMetering` in its activities), the focuser moving, a position outside the focuser's limits, a step of 0 over several positions, more than 999 positions.
2. Acquisition (`--acquisition-seconds`, no handover) and the folding mirror, as above.
3. `start_guiding`, then the settle in `status.latest.guiding` (MAST_unit#384). PHD2 reporting a failed settle, the guide start's record ending without one (the guider refused, PHD2 went away), a guider other than PHD2, or no settle within `--guide-settle-timeout` (120 s) end the run as `guiding_failed`. After the settle, guiding never steers the run.
4. At each position: `focuser/position`, a wait for the focuser's `Moving` to clear, `--settle-seconds`, then one status read for the measured position and the mount's pointing, then `--frames` flux frames.
5. The focuser goes to `known_as_good_position` on every way out once the unit was asked to do anything. **Guiding is left running**, as the in-unit run left it, so a second run is refused until guiding is stopped (`stop_acquisition_and_guiding`).

```
python -m flux_orchestrator.fiber_focus --unit mast03 --flux-meter http://mast-ns-spec:8003 --ra 1.967 --dec 28.52 --step 200 --count 11 --start 20500
```

**Products.** `<output-root>/<observing night>/FiberFocus/NNNN/`, the in-unit run's layout: `flux-<PPP>-<MMMMM>-<FF>.fits`, named by position index from 001, the **measured** focuser position and the frame within the position; headers carry `POSIDX`, `FRAMEIDX`, `FOCUSPOS`, `FOCUSCMD`, `FOCSETTL`, the mount's RA/Dec/HA/LMST and `OBJECT`. `result.json`, rewritten after every position, has the parameters, the commanded positions, the focuser limits, the guide settle, each position with its frames, and where the focuser was sent at the end.

**Stopping.** An operator's `unit/abort` is seen as `Aborting` in the unit's activities or tracking turned off, checked from the guide start on: in every wait, before each frame and after the last position. It ends the run as `aborted`, the frames already taken kept; the focuser is sent home once the abort has finished. **Ctrl+C** aborts the unit only while it acquires or moves the mirror; later it leaves guiding running. A refused acquisition request leaves nothing in flight and is not aborted, nor is an acquisition the unit reports failed or a mirror move that does not arrive; an acquisition this side gave up waiting on is. A failure mid-sweep (the flux meter, a focuser that does not arrive in 120 s) sends the focuser home and leaves the unit otherwise alone. A status read whose records failed is waited out until the settle timeout, whose message carries the unit's errors. The exit status is 0 only for a completed sweep.

**Differences from the in-unit run**, each forced by the API: the settle comes from the unit's record, which has PHD2's error text but not its numeric status, and stamps it on the unit's clock (`settled_unit_utc`); frames are written here, not on the unit's RAM disk; the unit cannot be claimed, so nothing stops another client using the focuser meanwhile; an abort is detected, not delivered.

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
src/flux_orchestrator/
  __main__.py                    CLI
  save_spirals.py                the save-spirals run and its CLI: frameless session, expose per cell, pairing
  fiber_focus.py                 the fiber-focus run and its CLI: guide, settle, focuser sweep, flux frames
  run.py                         one run: acquire, mirror, spiral walk, correlation, products
  unit_client.py                 the unit's public HTTP API
  flux_client.py                 this service's contract
  spiral.py                      the ring stopping rule, the median choice, the sky radius
  photometry.py                  one flux frame to net counts, with the hot-pixel guards
  correlate.py                   reference vs arg-max frame
  aperture_photometry_single.py  copied from MAST_unit
  frame_shift.py                 copied from MAST_unit
tests/
  fakes.py                       a unit and the service faked at the HTTP layer, sharing one sky
  test_orchestrator_run.py       whole runs against the fakes
  test_save_spirals.py           save-spirals runs, the clock offset and the pairing
  test_fiber_focus.py            fiber-focus runs: refusals, the settle, aborts, the focuser sent home
  test_orchestrator_rules.py     the stopping rule and the median
  test_orchestrator_photometry.py  detection, darkness and spikes
  test_service.py   endpoints in process against a stub camera
  test_save.py      /expose_and_save: layout, numbering, header, failures
  test_cli.py       CLI parsing, exit when the camera will not open
  test_contract.py  the client's exact requests against the service under uvicorn --fake
```

## Dependencies

FastAPI, uvicorn, numpy and astropy (for the FITS writer), pinned in `uv.lock`. The orchestrator adds httpx, photutils, scipy, scikit-image and tifffile (the last for the copied photometry module's own CLI). Tests use pytest. The Thorlabs SDK is installed separately (above). Nothing from MAST_common, MAST_unit or MAST_spec.
