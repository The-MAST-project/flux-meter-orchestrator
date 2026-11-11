"""`python -m flux_orchestrator --unit mastNN --flux-meter http://HOST:PORT [target] [options]`.

Run on mast-ns-spec, beside the flux-meter service. Ctrl+C aborts the run: during the acquisition the unit
is aborted; once the spiral session is requested it is closed, which stops tracking. Either way
`result.json` is written. Exit status 0 only for a converged run with a result.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from flux_orchestrator.flux_client import FluxMeterClient
from flux_orchestrator.run import DEFAULT_OUTPUT_ROOT, LOCAL_FRAMES_HELP, LOG_FORMAT, Run, RunParams
from flux_orchestrator.spiral import Terminal
from flux_orchestrator.unit_client import UNIT_PORT, Target, UnitClient

DEFAULTS = RunParams(unit="")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="flux_orchestrator", description="Find the pointing of maximum fiber flux.")
    parser.add_argument("--unit", required=True, help="the unit's host name, e.g. mast01")
    parser.add_argument("--unit-port", type=int, default=UNIT_PORT)
    parser.add_argument("--flux-meter", required=True, help="the flux-meter service, e.g. http://localhost:8003")

    where = parser.add_argument_group("target (coordinates win over a name; neither: where the telescope points)")
    where.add_argument("--ra", type=float, help="RA J2000, decimal hours")
    where.add_argument("--dec", type=float, help="Dec J2000, decimal degrees")
    where.add_argument("--object", help="a name the unit resolves")
    where.add_argument(
        "--skip-acquisition",
        action="store_true",
        help="shakedown only: walk wherever the mount points; dx/dy then means nothing",
    )

    run = parser.add_argument_group("run")
    run.add_argument("--seconds", type=float, default=DEFAULTS.seconds, help="imager and flux exposure")
    run.add_argument("--gain", type=int, help="imager gain (default: the unit's)")
    run.add_argument("--x-step", type=float, default=DEFAULTS.x_step_arcsec, help="RA step, arcsec on the sky")
    run.add_argument("--y-step", type=float, default=DEFAULTS.y_step_arcsec, help="Dec step, arcsec on the sky")
    run.add_argument("--max-rings", type=int, default=DEFAULTS.max_rings)
    run.add_argument("--patience-rings", type=int, default=DEFAULTS.patience_rings)
    run.add_argument("--max-radius", type=float, default=DEFAULTS.max_radius_arcsec, help="arcsec on the sky")
    run.add_argument("--frames", type=int, default=DEFAULTS.number_of_frames, help="flux frames per step (odd is best)")
    run.add_argument("--flux-gain", type=int, default=DEFAULTS.flux_gain)
    run.add_argument("--flux-black-level", type=int, default=DEFAULTS.flux_black_level)
    run.add_argument("--usable-fraction", type=float, default=DEFAULTS.usable_fraction)
    run.add_argument("--center", type=int, nargs=2, metavar=("X", "Y"), help="fiber position on the imager")

    paths = parser.add_argument_group("paths")
    paths.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help=f"default {DEFAULT_OUTPUT_ROOT}")
    paths.add_argument("--local-frames", action="store_true", help=LOCAL_FRAMES_HELP)

    args = parser.parse_args(argv)
    if (args.ra is None) != (args.dec is None):
        parser.error("--ra and --dec go together")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    params = RunParams(
        unit=args.unit,
        seconds=args.seconds,
        target=Target(ra_j2000_hours=args.ra, dec_j2000_degs=args.dec, object_name=args.object),
        gain=args.gain,
        x_step_arcsec=args.x_step,
        y_step_arcsec=args.y_step,
        max_rings=args.max_rings,
        patience_rings=args.patience_rings,
        max_radius_arcsec=args.max_radius,
        flux_gain=args.flux_gain,
        flux_black_level=args.flux_black_level,
        number_of_frames=args.frames,
        usable_fraction=args.usable_fraction,
        center=tuple(args.center) if args.center else None,
        skip_acquisition=args.skip_acquisition,
        local_frames=args.local_frames,
    )
    run = Run(
        params,
        UnitClient(args.unit, port=args.unit_port),
        FluxMeterClient(args.flux_meter),
        output_root=args.output_root,
    )
    record = run.run()
    result = record.result
    if result is None:
        logging.warning(f"{record.terminal}: no result ({record.no_result_reason}); see {record.folder}")
    else:
        logging.info(
            f"{record.terminal}: dx={result.dx} dy={result.dy} px (confidence {result.confidence}) at cell "
            f"{result.argmax_cell}; see {record.folder}"
        )
    return 0 if record.terminal == Terminal.CONVERGED and result is not None else 1


if __name__ == "__main__":
    sys.exit(main())
