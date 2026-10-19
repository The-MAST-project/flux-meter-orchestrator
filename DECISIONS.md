# Decisions

## [2026-10-19] The orchestrator drives the unit's operator spiral and reads its frames from the share

**Why.** Flux metering inside the unit reaches the mount, imager and stage directly, so it never tested what the unit's HTTP API can do, and the flux camera is not on the unit. The orchestrator was planned on 2026-10-13 around `expose` per step, with flux frames matched to each imager frame's window across the two hosts' clocks. That plan hit two walls: `expose` stops tracking after every run (MAST_unit#302, still open), and an `expose` inside an open spiral session overwrites the session's exposure series, after which the session cannot be closed until the unit service restarts (the overwrite in MAST_unit#232).

**What.**
- The spiral stays on the unit, through the operator spiral endpoints (`spiral_new_path` / `spiral_next_step` / `spiral_end_path`) rather than `acquire_and_find_max_flux`. The session saves one imager frame per step; the orchestrator never calls `expose`.
- Flux frames are taken after each step returns, at the settled cell, `N` per step reduced by the median, so no frame pairing across the hosts' clocks is needed. One imager frame per step, not `N`.
- The camera stays behind this service; the orchestrator is a client of two HTTP APIs and opens no hardware. Supersedes the 2026-10-13 entry's note on the API gaps (`MAST_unit#326`, `#327`): the acquisition outcome now comes from MAST_unit#346, and the spiral session's gaps were fixed in MAST_unit#349 (share paths relative to the share's MAST root, a failed step frame reported, the abandonment timer re-armed per step).
- The reduction (aperture photometry, the median, the ring rule, the correlation) is copied into `flux_orchestrator`; the photometry and frame-shift modules nearly verbatim, so they stay comparable with the unit's until those are removed. The two copies are exempt from ruff's C901 and from pyright, whose complaints there come from third-party typing.
- New dependencies: httpx (moved from dev), photutils, scipy, scikit-image, tifffile.

**Implications.** The reduction exists twice until MAST_unit's flux metering is removed. A run needs a unit with #346 and #349. The correlation uses one frame at the arg-max cell, against the in-unit procedure's choice among `N`.

## [2026-10-13] Hosted on a personal GitHub repo; the flux-metering orchestrator lives here for now

**Why.** Flux metering is moving out of the unit service into an orchestrator that drives a unit over its HTTP API and this service over its own. That orchestrator needs a home before its durable one is decided, and the work should not add a repository to The-MAST-project while it is still exploratory.

**What.**
- The repository is pushed to `elibrody-weizmann/flux-meter`, private, rather than kept local-only.
- The orchestrator is built here, beside the service, as a separate package. It is a client of both APIs and does not open the camera itself; the service stays the camera's only owner.
- The unit-side API gaps it depends on are tracked in MAST_unit: the acquisition's outcome in status (`MAST_unit#326`) and `expose` reporting each frame's real window (`MAST_unit#327`).

**Implications.** A move to a durable home, in The-MAST-project or elsewhere, is a later decision; until then this repository is not visible to the rest of the team unless shared.

## [2026-10-13] `/expose_and_save` files a frame on the share as numbered FITS

**Why.** An operator taking a single flux frame through `/expose` got `.npy` bytes in the browser, which is the units' wire format, not a file anyone can open as an image.

**What.**
- `POST /expose_and_save` takes one frame with the current settings and writes it as FITS to `<save-root>/<observing night>/flux-meter/flux-NNNN.fits`, returning `path` and `sequence`. `/expose` is unchanged.
- `--save-root` defaults to the UNC path `\\mast-ns-control\mast-share\MAST\mast-ns-spec`, beside the spectrographs' folders; a UNC path rather than a drive letter, because a mapped drive belongs to one logon session.
- The night is the UTC date twelve hours earlier, MAST_common's `observing_night`, reimplemented here because the service does not depend on MAST_common. Numbering continues from the highest number in the night's folder, so a restart never reuses one. The file is written under a hidden `.part` name and renamed into place.
- The camera reports no settings of its own, so the header records the exposure, gain and black level last set through `/configure`, and omits them when none was set since startup, rather than guessing.
- Numbering and writing take their own lock, so a slow share never holds the camera lock the units' `/expose` needs.
- astropy is added as a dependency for the writer.

**Implications.** A frame saved between two units' runs uses whatever the last run configured. The share must be reachable from the service's logon session.

## [2026-10-12] A standalone service holds the Zelux on mast-ns-spec

**Why.** The Zelux flux camera hangs off `mast-ns-spec`, not off the unit that runs flux metering, so the unit needs to reach it over the network. The unit-side client (`RemoteFluxMeter` in MAST_unit) was written first against a fixed three-endpoint contract: `GET /info`, `PUT /configure`, `POST /expose`.

**What.**
- A standalone FastAPI service, `flux_meter_service`, with its own copy of `ThorCam` (stdlib logging, its own `FluxMeterError`). No dependency on MAST_common, MAST_unit or MAST_spec, so it installs and runs on the spec host without the MAST tree.
- The camera is opened in the app's lifespan and held for the process lifetime. A camera that will not open fails startup and the process exits, rather than serving 503s forever. No reconnect: after an unplug the operator restarts the service.
- No open/close endpoints, no claim and no token. A `threading.Lock` serializes every camera call. One run at a time is an operating rule, not something the service enforces.
- `--port` is required on the CLI with no default. The units hard-code it as the units' `FLUX_METER_PORT` (8003, in MAST_unit `src/flux_metering/remote.py`), not in the MAST configuration, because the arrangement is a stopgap; a default here would be a second source of truth.
- Configure refusals are 400 and expose failures 503, both as FastAPI `{"detail": ...}`, which is what the client reads.
- The Thorlabs SDK is not a declared dependency: it is not on PyPI. It is imported lazily by name, and `--dll-dir` adds its native DLLs to the search path on Windows.
- A local-only git repository for now, with no remote. Superseded the next day by the 2026-10-13 hosting entry.

**Implications.**
- The unit's `thorcam.py` and this copy can drift until the unit's copy is retired.
- Two runs at once would interleave their configure and expose calls; nothing here stops that.
- Moving the repository to The-MAST-project, and running it as a Windows service rather than by hand, are open.
