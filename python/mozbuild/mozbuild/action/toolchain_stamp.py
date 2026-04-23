# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Hash toolchain identity into a stamp file.

Used by the ninja backend to invalidate compile outputs when the
underlying toolchain changes. Cargo does this for rust automatically
via its per-crate fingerprint; this is the C/C++/asm equivalent.

Each command-line argument after the output is either:

  * A file path — its (size, mtime_ns) feeds the digest.
  * A directory path — every entry name in the directory feeds the
    digest. This is the cheap-yet-complete signal for the mozbuild
    toolchains tarball directory, where each filename is prefixed
    with the TaskCluster artifact hash. Listing the directory is a
    full version manifest at one syscall's cost; we do NOT recurse,
    so the action stays fast even for thousands of nested files.

The output file is rewritten ONLY when the digest differs from its
existing content, which preserves mtime on no-change builds. Combined
with the consuming ninja edge's `restat=1`, downstream consumers stay
idle when toolchain identity is unchanged.

argv: ``OUTPUT INPUT [INPUT ...]``
"""

import hashlib
import os
import sys
from pathlib import Path


def _entry(path):
    """Return a stable identity string for ``path``.

    For files: ``size`` + ``mtime_ns``.
    For directories: a sorted list of immediate-child names. We don't
    stat each child — the directory listing alone is sufficient when
    the children are themselves version-stamped (TaskCluster artifact
    tarballs).
    """
    try:
        st = os.stat(path)
    except OSError as e:
        return "%s\tERR:%s" % (path, e.errno)
    if not Path(path).is_dir():
        return "%s\tFILE\t%d\t%d" % (path, st.st_size, int(st.st_mtime_ns))
    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        return "%s\tDIR_ERR:%s" % (path, e.errno)
    return "%s\tDIR\t%s" % (path, "\t".join(names))


def main(argv):
    if len(argv) < 2:
        print(
            "usage: toolchain_stamp OUTPUT INPUT [INPUT ...]",
            file=sys.stderr,
        )
        return 2
    output = argv[0]
    inputs = argv[1:]

    h = hashlib.sha1()
    for path in sorted(inputs):
        h.update(_entry(path).encode("utf-8") + b"\n")
    digest = h.hexdigest()

    try:
        existing = Path(output).read_text(encoding="utf-8").strip()
    except OSError:
        existing = None

    if existing == digest:
        return 0
    Path(output).write_text(digest + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
