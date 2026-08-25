# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Install the staged application tree under a prefix."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional

import buildconfig
import mozpack.path as mozpath

from mozbuild.packaging import PackagePaths

_UNSUPPORTED = ("WINNT", "Darwin")
_EXCLUDES = ("--exclude=precomplete", "--exclude=.mkdir.done")


def package_install(
    *,
    destdir: str = "",
    installdir: Optional[str] = None,
    bindir: Optional[str] = None,
    pkg_dir: Optional[str] = None,
) -> int:
    substs = buildconfig.substs
    if substs.get("OS_TARGET") in _UNSUPPORTED:
        print(
            "Installing is not supported on this platform. Build the package instead.",
            file=sys.stderr,
        )
        return 1

    dist = mozpath.join(buildconfig.topobjdir, "dist")
    paths = PackagePaths(substs=substs, dist_dir=dist, pkg_dir=pkg_dir)
    staged = mozpath.join(dist, paths.pkg_dir)
    app_name = substs["MOZ_APP_NAME"]
    installdir = installdir or f"{substs['libdir']}/{app_name}"
    bindir = bindir or substs["bindir"]
    tar = substs["TAR"]

    target = f"{destdir}{installdir}"
    os.makedirs(target, exist_ok=True)

    create = subprocess.Popen(
        [tar, *_EXCLUDES, "-chf", "-", "."], cwd=staged, stdout=subprocess.PIPE
    )
    extract = subprocess.Popen([tar, "-xf", "-"], cwd=target, stdin=create.stdout)
    create.stdout.close()
    extract.wait()
    create.wait()
    if create.returncode:
        return create.returncode
    if extract.returncode:
        return extract.returncode

    bin_target = f"{destdir}{bindir}"
    os.makedirs(bin_target, exist_ok=True)
    link = mozpath.join(bin_target, app_name)
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(f"{installdir}/{app_name}", link)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install the staged application tree under a prefix."
    )
    parser.add_argument(
        "--destdir",
        default="",
        metavar="DIR",
        help="Prefix every installed path with this staging directory",
    )
    parser.add_argument(
        "--installdir", metavar="DIR", help="Directory the application is installed in"
    )
    parser.add_argument(
        "--bindir", metavar="DIR", help="Directory the launcher symlink is created in"
    )
    parser.add_argument("--pkg-dir", help="Staged package directory name")
    return parser


def main(argv: list[str]) -> int:
    return package_install(**vars(_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
