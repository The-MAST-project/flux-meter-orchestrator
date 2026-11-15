# Decisions

## [2026-11-15] An interactive app over the runs

**Why.** Param runs the orchestrator by hand on mast03. Three command lines with a dozen flags each, products found by path and Ctrl+C as the only stop are workable for a developer and slow for an operator.

**What.** `python -m flux_orchestrator.app`, on Textual (approved 2026-11-15), over the three runs unchanged. A run gets two optional hooks. `on_event` receives immutable `RunEvent` snapshots at phase changes, steps and frames, through a `Reporter` that swallows a watcher's failure, so the display cannot change how a run ends. A per-run `StopToken`, checked in every unit-client wait and between frames and steps, raises `StopRequested` once: a `KeyboardInterrupt`, so each run's existing Ctrl+C handling applies unchanged. A run closes its token as it leaves its work, before any cleanup, so neither that stop nor one pressed later cuts the cleanup (focuser home, abort confirmed) short; `stopped_by` names a stop only when one actually ended the run (raised, or a hard stop). The command line keeps plain Ctrl+C. A second stop is a hard stop that also sends `unit/abort`, recorded as `stopped_by` in the run's record; it is the app's only call that acts on the unit. The status panel reads the unit through a client of its own; the camera's `/info` is read once and after each run, since it waits behind an exposure. One session per host, by an OS file lock on `~/.flux-orchestrator.lock` that dies with the process. Ctrl+Q and the command palette's Quit are refused mid-run like `q`: a closed app would leave its run going with no screen and no stop. Session settings in JSON, overrides only.

**Implications.** Over ssh a dropped connection ends the app and its run without cleanup (accepted 2026-11-15; long runs from the unit's desktop). A stop lands only at the next check: inside one long call (a spiral step, an exposure, a max-flux measurement wait) it waits for that call, which is what the hard stop is for.

## [2026-11-15] Public, as the org's other repositories are

**Why.** Units clone the orchestrator to install it (README), and a private repository would need credentials on every unit; MAST\_unit, MAST\_common and MAST\_provisioning are public.

**What.** **Supersedes** the *Implications* of the entry below on visibility: the repository is public. The history was checked first for credentials, keys, vendor binaries and internal addresses; it holds none.

**Implications.** Nothing secret goes in here: hosts and ports already public in MAST\_unit are fine, credentials never are.

## [2026-11-15] Moved to The-MAST-project as flux-meter-orchestrator

**Why.** The orchestrator is being handed to Param to run flux metering on mast03, which needs a repository the team can reach, and MAST work belongs on The-MAST-project, not on personal forks.

**What.** **Supersedes** the 2026-10-13 entry "Hosted on a personal GitHub repo". The full history is pushed to `The-MAST-project/flux-meter-orchestrator`, private; `elibrody-weizmann/flux-meter` is no longer pushed to. The repository is named for both halves; the packages keep their names.

**Implications.** Units install the orchestrator from a clone of the new repository (README, *Install the orchestrator on a unit*). Making it public, as the org's other repositories are, is a separate decision.

## [2026-11-15] The fiber-focus run, off the unit

**Why.** Param's fiber-focus run exists only on MAST_unit `eli/flux-night`; moving mast03 back to `main` needs it here, as the save-spirals run already is.

**What.** `fiber_focus.py` ports it through the public API. The settle is read from `status.latest.guiding` (MAST_unit#384) rather than PHD2's events; the focuser's `Moving` activity, the completion the position endpoints declare, is the arrival signal, so the unit's close-enough tolerance stays in one place. An operator's `unit/abort` cannot be delivered to a run off the unit, so it is detected: `Aborting` in the unit's activities or tracking off, checked from the guide start on, including while a focuser move is awaited, between frames and after the last position. Exits follow the in-unit run: only Ctrl+C during the acquisition or mirror move, or an acquisition this side gave up on once the unit had accepted it, aborts the unit, so a refused request never stops another client's work; every other ending sends the focuser home and leaves guiding running. The up-front refusals are the in-unit run's, read from status.

**Implications.** A second run is refused while the first's guiding runs. Detecting an abort by tracking means a client that turns tracking off mid-sweep ends the run too. PHD2's numeric settle status is not recorded.

## [2026-11-15] `--local-frames` dropped

**Why.** `--local-frames` (added 2026-11-11, for a unit whose share is demoted) rested on a wrong premise: while the share is demoted, the unit's mover still takes every frame off the RAM disk within about a second, to `C:/MAST/share-fallback/`, and spiral steps report `image: null`, so a read from `local_products_path` finds nothing. The fake unit did not model the mover, so the tests passed. The share is back, so the option is not needed.

**What.** The option and its 2026-11-11 entry are reverted; a demoted share is refused again, as before.

**Implications.** A run against a unit whose share is down waits for the share. If that case comes back, it starts from the fallback folder, not from the RAM disk.

## [2026-11-04] The save-spirals run reads the commanded RA step from `status.latest.spiral`

**Why.** The frameless `spiral_new_path` answers with no `ra_step_commanded_arcsec`, so the 2026-10-29 entry dropped the check for on-sky spiral steps (MAST_unit#136) and leaned on merge order instead: a unit with frameless sessions has #136 too. That holds for `main` but is an inference, not something the unit says.

**What.** `status.latest.spiral` reports `ra_step_commanded_arcsec` and `dec_for_ra_step_degs` for either kind of session (MAST_unit#391, with MAST_common#217), and the save-spirals run refuses a session whose record lacks them, as the max-flux run refuses a response without them. **Supersedes** the last sentence of the 2026-10-29 entry's *Implications*.

**Implications.** Until that change is on a unit, the save-spirals run refuses it right after opening the session, and aborts it.

## [2026-10-29] The save-spirals run: a frameless session, `expose` per cell, pairing across the clocks

**Why.** Param's save-spirals run (`acquire_and_save_spirals`, in the unit's flux-metering branch) starts an imager frame and a flux frame together at every cell, which the in-unit code can do and the operator spiral cannot: it takes exactly one frame per step. The spiral logic stays in the unit, one point of reference for every spiral-like process, so the unit gained a session that only moves (MAST_unit#385) and per-frame records in `status.latest.exposure` (MAST_unit#327). Param does not read the run's `result.json`, so its layout is the orchestrator's own.

**What.**
- A separate command, `python -m flux_orchestrator.save_spirals`, with its own products folder (`save-spirals/NNNN`) and record; the max-flux run is unchanged.
- At each cell `expose` takes the imager frames into a subfolder of their own; flux frames run back to back from `expose`'s answer until the unit's record of the series ends, and each imager frame is paired with the flux frame overlapping it most.
- The clock offset is bracketed at every cell by `expose`'s round trip around the record's `started`, rather than measured once with `w32tm` before the run as the 2026-10-13 plan had it: it uses only the public API, needs no NTP service on the unit, and tracks drift through the run.
- A failed imager frame is recorded and the walk goes on, as `expose` itself does; the in-unit run stopped on one. A series that saved no frame (the imager gone) fails the run rather than walking the rest of the spiral empty, and so does a demoted share, checked on the first read of the series rather than after it.
- An interruption or failure aborts the unit (`unit/abort`, which closes the session) rather than calling `spiral_end_path`, which refuses while a series is in flight. A `spiral_end_path` that fails is a failure, so it aborts too: a frameless session left open keeps the mount tracking until the unit's session timeout. `unit/abort` answers before it has stopped anything, so the run reads `latest.spiral` and `latest.exposure` until both have ended and records whether they did, rather than taking the answer as the release.
- The flux frames' shared header cards moved into `run.flux_frame_header`, used by both runs.

**Implications.** The pairing depends on PHD2's `DATE-OBS` being the frame's real start, true when PHD2 is not guiding; a frame it hands back while guiding is flagged, not paired. The imager frames' gain defaults to `expose`'s, not the acquisition's. A run needs a unit with MAST_unit#385, #327 and #136 together. The frameless `spiral_new_path` does not report the RA step it commanded, so the run does not check for #136 as the max-flux run does: #385 merged after #136, and a session that reports `frames` false comes from a unit that has both.

## [2026-10-29] The spiral session's share folder comes from `status.latest.spiral`

**Why.** MAST_unit#349 first reported the session folder and step frames relative to the share's MAST root, because the share was spelled differently on each host. Since MAST_common#200 every Windows host reaches the share by the same UNC path, `//mast-ns-control/mast-share/MAST/`, mast-ns-spec included, and the unit's run records (`status.latest.*`, MAST_common `LatestBase`) already carry each run's folder on the share. #349 was reworked onto a `latest.spiral` record (paired with MAST_common#212).

**What.** Supersedes the share-path part of the 2026-10-19 entry. The orchestrator reads the session folder from `status.latest.spiral.shared_products_path` after `spiral_new_path`, and uses that path and each step's `image` as they are. `--share-root` is removed. A unit that does not report `latest.spiral`, or reports no share folder (its share is demoted), is refused and the session it opened is closed.

**Implications.** A run reads the unit's frames by their UNC paths, so it must run on a Windows host on the share, as mast-ns-spec is. `result.json` records the `latest.spiral` record as the session opened.

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
