# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Generic single-edge executor for build backends.

Runs the steps in a JSON spec in order, stopping on the first failure,
then optionally touches a stamp. Invoked as one command so it behaves
identically on POSIX and Windows without relying on a shell. Each step
runs as its own subprocess: ``module`` steps re-launch the current
interpreter with ``-m <module>``; ``exec`` steps run the given argv
directly.
"""

import argparse
import subprocess
import sys
from pathlib import Path

from mozfile import json


def _run_step(step):
    if "module" in step:
        argv = [sys.executable, "-m", step["module"], *step.get("args", [])]
    else:
        argv = step["exec"]
    return subprocess.run(argv, check=False).returncode


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    args = parser.parse_args(argv)

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)

    for step in spec["steps"]:
        rc = _run_step(step)
        if rc:
            sys.stderr.write(
                f"ninja_run: step failed (rc={rc}): "
                f"{spec.get('description') or args.spec}\n"
            )
            return rc

    stamp = spec.get("stamp")
    if stamp:
        path = Path(stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
