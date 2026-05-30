# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# Install objdir files, preserving each file's mode (e.g. the executable bit)
# and allowing installation under a different name. Such installs are expressed
# in moz.build and run as this build action, rather than going through the
# legacy make/nsinstall install path.
#
# Accepts a single `<source> <dest>` pair or a `<manifest>` file of
# tab-separated `source<TAB>dest` lines (batched to amortize Python startup
# across many installs). With `--hardlink`, files are hardlinked (falling back
# to a copy) instead of copied.

import argparse
import os
import shutil
import sys


def _install_one(source, dest, hardlink):
    dest_dir = os.path.dirname(dest)
    if dest_dir:
        os.makedirs(dest_dir, exist_ok=True)
    if hardlink:
        if os.path.lexists(dest):
            try:
                os.unlink(dest)
            except OSError:
                pass
        try:
            os.link(source, dest)
        except OSError:
            shutil.copy2(source, dest)
    else:
        shutil.copy(source, dest)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--hardlink",
        action="store_true",
        help="hardlink each file (falling back to a copy) instead of copying",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        help="a <source> <dest> pair, or a single <manifest> of "
        "tab-separated source<TAB>dest lines",
    )
    args = parser.parse_args(argv)

    if len(args.paths) == 1 and args.paths[0].endswith(".manifest"):
        with open(args.paths[0], encoding="utf-8") as fh:
            for line in fh:
                stripped = line.rstrip("\r\n")
                if not stripped:
                    continue
                source, _, dest = stripped.partition("\t")
                _install_one(source, dest, args.hardlink)
        return 0
    if len(args.paths) == 2:
        _install_one(args.paths[0], args.paths[1], args.hardlink)
        return 0
    parser.error("expected a <source> <dest> pair or a single <manifest>")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
