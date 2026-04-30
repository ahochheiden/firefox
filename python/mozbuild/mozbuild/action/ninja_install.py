# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Install one or many files via hardlink (fall back to copy).

Used by the ninja backend's `install_file` rule (single-pair) and
`install_batch` rule (reads a manifest file containing one `src:dst`
per line). Hardlinks are near-instant on NTFS and avoid the per-file
Python startup cost that a copy-per-edge would incur across hundreds
of EXPORTS entries.

The batch path parallelizes via mozpack.copier.parallel_apply so a
single mega-edge with thousands of inputs scales across all available
cores instead of bottlenecking on a single-threaded for-loop.
"""

import os
import shutil
import sys

from mozpack.copier import parallel_apply


def _install_one(pair):
    src, dst = pair
    dst_dir = os.path.dirname(dst)
    if dst_dir:
        os.makedirs(dst_dir, exist_ok=True)
    if os.path.lexists(dst):
        try:
            os.unlink(dst)
        except OSError:
            pass
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main(argv):
    if len(argv) == 1 and argv[0].endswith(".manifest"):
        pairs = []
        with open(argv[0]) as f:
            for line in f:
                stripped = line.rstrip("\r\n")
                if not stripped:
                    continue
                src, _, dst = stripped.partition("\t")
                pairs.append((src, dst))
        parallel_apply(pairs, _install_one)
        return 0
    if len(argv) == 2:
        _install_one((argv[0], argv[1]))
        return 0
    print("usage: ninja_install <src> <dst> | <manifest>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
