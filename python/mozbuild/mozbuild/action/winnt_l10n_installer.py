# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Per-locale Windows installer dispatch.

Runs ``$MAKE -C <topobjdir>/<app>/locales package-win32-installer
AB_CD=<locale>`` (the Part 14 makefile cascade's trailing
``ifeq (WINNT,$(OS_ARCH))`` branch).

The Windows installer is still produced by `browser/installer/windows`
make rules (NSIS + helper.exe). Once those move into a Python action /
ninja edges, this dispatch action goes away.
"""

import argparse
import subprocess
import sys


def main(argv):
    parser = argparse.ArgumentParser(
        description=("Dispatch package-win32-installer to make for a single locale.")
    )
    parser.add_argument("--locale", required=True, help="The ab_cd locale code")
    parser.add_argument(
        "--make", required=True, help="Path to the configured make binary"
    )
    parser.add_argument(
        "--locales-dir",
        required=True,
        help=(
            "Path to the app's locales objdir (e.g. "
            "<topobjdir>/browser/locales) where the package-win32-installer "
            "target lives."
        ),
    )
    args = parser.parse_args(argv)

    rc = subprocess.call([
        args.make,
        "-C",
        args.locales_dir,
        "package-win32-installer",
        f"AB_CD={args.locale}",
    ])
    if rc:
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
