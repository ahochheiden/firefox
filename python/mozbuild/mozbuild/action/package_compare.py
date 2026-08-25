# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Report the difference between the package manifest and the staged tree."""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from io import StringIO
from typing import Optional, Sequence, TextIO

import buildconfig
import mozpack.path as mozpath

from mozbuild.action import preprocessor


def _manifest_files(manifest: str, defines: Sequence[str], binpath: str) -> list[str]:
    out = StringIO()
    preprocessor.generate(out, *[f"-D{define}" for define in defines], manifest)
    return sorted(
        line for line in out.getvalue().splitlines() if line.startswith(f"{binpath}/")
    )


def _staged_files(source: str, binpath: str, follow_symlinks: bool) -> list[str]:
    files = []
    root = mozpath.join(source, binpath)
    if not os.path.isdir(root):
        raise NotADirectoryError(root)
    for dirpath, _, names in os.walk(root, followlinks=follow_symlinks):
        for name in names:
            path = os.path.join(dirpath, name)
            if not os.path.isfile(path):
                continue
            if not follow_symlinks and os.path.islink(path):
                continue
            files.append(mozpath.relpath(mozpath.normsep(path), source))
    return sorted(files)


def package_compare(
    *,
    manifest: str,
    source: Optional[str] = None,
    binpath: Optional[str] = None,
    defines: Sequence[str] = (),
    output: Optional[TextIO] = None,
) -> int:
    substs = buildconfig.substs
    source = source or mozpath.join(buildconfig.topobjdir, "dist")
    binpath = binpath or substs["MOZ_PKG_BINPATH"]
    follow_symlinks = substs.get("OS_ARCH") != "WINNT"

    try:
        staged = _staged_files(source, binpath, follow_symlinks)
    except NotADirectoryError as e:
        print(f"No staged tree to compare at {e.args[0]}", file=sys.stderr)
        return 1

    for line in difflib.unified_diff(
        _manifest_files(manifest, defines, binpath),
        staged,
        fromfile="package manifest",
        tofile="staged tree",
        lineterm="",
    ):
        print(line, file=output or sys.stdout)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report the difference between the package manifest and the "
        "staged tree."
    )
    parser.add_argument("--manifest", required=True, help="Package manifest file")
    parser.add_argument("--source", metavar="DIR", help="Directory holding the tree")
    parser.add_argument(
        "--binpath", metavar="PATH", help="Binary path inside the staged tree"
    )
    parser.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        metavar="VAR[=VAL]",
        help="Define a variable. Repeatable, and later definitions win.",
    )
    return parser


def main(argv: list[str]) -> int:
    return package_compare(**vars(_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
