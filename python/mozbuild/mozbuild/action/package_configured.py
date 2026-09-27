# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Package the application with the options the build configuration selects."""

from __future__ import annotations

import argparse
import os
import sys

import buildconfig

from mozbuild.action.package_application import _MODES, package_application
from mozbuild.packaging import package_options


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Package the application with the options the build "
        "configuration selects."
    )
    parser.add_argument(
        "--mode",
        choices=_MODES,
        default="package",
        help="Stop after staging, install under a prefix, or build the package",
    )
    parser.add_argument(
        "--ab-cd", help="The locale being packaged. Defaults to MOZ_UI_LOCALE."
    )
    args = parser.parse_args(argv)

    return package_application(
        **package_options(
            substs=buildconfig.substs,
            topobjdir=buildconfig.topobjdir,
            environ=os.environ,
            mode=args.mode,
            ab_cd=args.ab_cd,
        )
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
