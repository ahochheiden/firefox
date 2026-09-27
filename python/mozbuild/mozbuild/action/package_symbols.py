# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Archive the crash reporter symbols, or upload the full archive."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import buildconfig
import mozpack.path as mozpath

from mozbuild.action import symbols_archive
from mozbuild.packaging import PackagePaths

_UPLOAD_SYMBOLS = "toolkit/crashreporter/tools/upload_symbols.py"


def _archive(path: str, symbols: str, *extra: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    symbols_archive.main([path, symbols, *extra])


def package_symbols(*, upload: bool = False) -> int:
    substs = buildconfig.substs
    dist = mozpath.join(buildconfig.topobjdir, "dist")
    paths = PackagePaths(substs=substs, dist_dir=dist)
    full = bool(os.environ.get("MOZ_ENABLE_FULL_SYMBOLS"))
    full_archive = mozpath.join(
        paths.pkg_output_dir, f"{paths.symbol_full_archive_basename}.tar.zst"
    )

    if upload:
        if not full:
            return 0
        return subprocess.call([
            sys.executable,
            "-u",
            mozpath.join(buildconfig.topsrcdir, _UPLOAD_SYMBOLS),
            full_archive,
        ])

    if not full and not substs.get("MOZ_CRASHREPORTER"):
        print("Skipping symbols generation because MOZ_CRASHREPORTER is not set.")
        return 0

    print("packing symbols")
    os.makedirs(paths.pkg_output_dir, exist_ok=True)
    symbols = mozpath.abspath(mozpath.join(dist, "crashreporter-symbols"))
    # Set MOZ_ENABLE_FULL_SYMBOLS to enable generation and upload of the full
    # crashreporter symbols archives
    if full:
        _archive(full_archive, symbols, "--full-archive")
    _archive(
        mozpath.join(paths.pkg_output_dir, f"{paths.symbol_archive_basename}.zip"),
        symbols,
    )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Archive the crash reporter symbols, or upload the full archive."
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload the full symbols archive instead of writing the archives",
    )
    return package_symbols(**vars(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
