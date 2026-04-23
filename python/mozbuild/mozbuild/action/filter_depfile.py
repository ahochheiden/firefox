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
  3. Rewrites deps under topobjdir to topobjdir-relative form so they
     match the relative paths NinjaBackend writes on its build edges
     (`_rel_n_path`). Without this, a depfile entry like
     `<topobjdir>/dist/bin/gkcodecs.dll` and a build edge producing
     `dist/bin/gkcodecs.dll` resolve to two different ninja graph
     nodes, and ninja reports "no known rule" on the absolute one.

Usage: python -m mozbuild.action.filter_depfile <depfile> <topobjdir>
"""

import os
import re
import sys

import mozpack.path as mozpath

_WILDCARD_RE = re.compile(r"\$\(wildcard ([^)]*)\)")


def _make_relative(dep, topobjdir):
    norm = mozpath.normsep(dep)
    if norm == topobjdir:
        return "."
    if norm.startswith(topobjdir + "/"):
        return norm[len(topobjdir) + 1 :]
    return norm


def main(argv):
    if len(argv) != 2:
        print("usage: filter_depfile <depfile> <topobjdir>", file=sys.stderr)
        return 2
    path, topobjdir = argv
    topobjdir = mozpath.normsep(topobjdir)
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
    kept = [_make_relative(d, topobjdir) for d in kept]

    new_text = f"{target}: {' '.join(kept)}\n"

    with open(path, "w") as f:
        f.write(new_text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
