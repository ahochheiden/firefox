# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Post-process a Makefile-style depfile so ninja can consume it cleanly.

`mozbuild.action.file_generate` emits depfiles that wrap optional deps as
`$(wildcard path)`. GNU make expands those to empty when the path is
missing. Ninja's gcc-depfile parser treats them as literal path tokens
and then sees the missing file, marking the edge dirty on every build.

This filter:

  1. Unwraps `$(wildcard PATH)` to bare `PATH`.
  2. Drops any dependency whose path does not exist on disk, so ninja
     does not re-run the rule purely because a conditional input is
     absent.

Usage: python -m mozbuild.action.filter_depfile <depfile>
"""

import os
import re
import sys

_WILDCARD_RE = re.compile(r"\$\(wildcard ([^)]*)\)")


def main(argv):
    if len(argv) != 1:
        print("usage: filter_depfile <depfile>", file=sys.stderr)
        return 2
    path = argv[0]
    if not os.path.exists(path):
        # No depfile -> nothing to filter. Not an error.
        return 0
    with open(path) as f:
        text = f.read()

    text = _WILDCARD_RE.sub(r"\1", text)

    if ":" not in text:
        return 0
    target, _, rest = text.partition(":")
    deps = rest.split()
    kept = [d for d in deps if os.path.exists(d)]
    new_text = f"{target}: {' '.join(kept)}\n"

    with open(path, "w") as f:
        f.write(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
