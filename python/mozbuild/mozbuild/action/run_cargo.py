# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run Cargo for one serialized Rust build edge."""

import argparse
import os
import subprocess
import sys
from pathlib import Path

from mozfile import json

from mozbuild.rust_commands import (
    CargoCommand,
    compose_cargo_build_edge_argv,
    compose_env,
)


def main(argv):
    parser = argparse.ArgumentParser(
        description="Compose and run Cargo for a Rust build edge."
    )
    parser.add_argument("--spec", required=True, type=Path)
    # These flags vary per invocation and are not stored in the edge spec.
    parser.add_argument("--timings", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--single-job", action="store_true")
    args = parser.parse_args(argv)

    command = CargoCommand.from_dict(json.loads(args.spec.read_text(encoding="utf-8")))

    import buildconfig

    from mozbuild.backend.configenvironment import PartialConfigDict

    # Read configure substitutions without environment overrides. The current
    # environment is passed separately and may already contain composed RUSTFLAGS.
    substs = PartialConfigDict(
        os.path.join(buildconfig.topobjdir, "config.statusd"), "substs"
    )

    env = compose_env(
        command,
        substs,
        os.environ,
        buildconfig.topsrcdir,
        buildconfig.topobjdir,
    )
    cargo_argv = compose_cargo_build_edge_argv(
        command,
        substs,
        env,
        timings=args.timings,
        keep_going=args.keep_going,
        single_job=args.single_job,
    )

    # Keep jobserver file descriptors open for Cargo.
    return subprocess.run(
        cargo_argv,
        env=env,
        cwd=command.working_directory or None,
        close_fds=False,
        check=False,
    ).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
