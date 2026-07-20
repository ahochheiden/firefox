# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Run makensis for a staged Windows installer."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def nsis_build(
    config_dir: Path,
    nsi: str,
    makensis: str,
    makensis_flags: list[str],
    produced: str,
    output: str,
) -> int:
    if result := subprocess.run(
        [makensis] + list(makensis_flags) + [nsi], cwd=config_dir, check=False
    ).returncode:
        return result

    if output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config_dir / produced, out)

    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Run makensis on an .nsi script in a staged CONFIG_DIR."
    )
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument("--nsi", required=True, help="The .nsi script to compile")
    parser.add_argument("--makensis", required=True, help="MAKENSISU")
    parser.add_argument(
        "--makensis-flag",
        action="append",
        default=[],
        dest="makensis_flags",
        help="A flag to pass to makensis (e.g. -nocd). Repeatable.",
    )
    parser.add_argument(
        "--produced", default="", help="Name of the exe makensis writes in CONFIG_DIR"
    )
    parser.add_argument(
        "--output", default="", help="Copy the produced exe to this path"
    )
    args = parser.parse_args(argv)

    return nsis_build(
        config_dir=args.config_dir,
        nsi=args.nsi,
        makensis=args.makensis,
        makensis_flags=args.makensis_flags,
        produced=args.produced,
        output=args.output,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
