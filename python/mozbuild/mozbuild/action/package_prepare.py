# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Prepare a staged application tree for archiving."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional, Sequence

import mozpack.path as mozpath

from mozbuild.action import find_dupes, package_libmegazord_artifacts
from mozbuild.action import zip as zip_action

_REWRITE_SANITIZER_DYLIB = "build/unix/rewrite_sanitizer_dylib.py"


def check_symbol_archives(symbol_archives: Sequence[str]) -> int:
    for archive in symbol_archives:
        if not os.path.exists(archive):
            print(
                f"Expected symbol archive {archive} not found. This should have "
                "been created by artifacts.py during artifact installation. "
                "Check that UPLOAD_DIR is set correctly and that symbols were "
                "downloaded.",
                file=sys.stderr,
            )
            return 1
    return 0


def package_prepare(
    *,
    staged_dir: str,
    topsrcdir: Optional[str] = None,
    symbol_archives: Sequence[str] = (),
    check_dupes: bool = False,
    defines: Sequence[str] = (),
    warn_dupes: bool = False,
    allowed_dupes: Sequence[str] = (),
    mozharness_zip: Optional[str] = None,
    jsshell: Optional[Sequence[str]] = None,
    rewrite_sanitizer_dylib: Optional[str] = None,
    archives: Sequence[Sequence[str]] = (),
    libmegazord_artifacts: Optional[Sequence[str]] = None,
) -> int:
    rc = check_symbol_archives(symbol_archives)
    if rc:
        return rc

    if check_dupes:
        argv = [f"-D{define}" for define in defines]
        if warn_dupes:
            argv.append("-w")
        for allowlist in allowed_dupes:
            argv += ["-f", allowlist]
        rc = find_dupes.main([*argv, staged_dir])
        if rc:
            return rc

    if mozharness_zip:
        from mozbuild.action import test_archive

        test_archive.main(["mozharness", mozharness_zip])

    if jsshell:
        source, output, files_from = jsshell
        if os.path.exists(output):
            os.remove(output)
        zip_action.main([
            "-C",
            source,
            "--strip",
            os.path.abspath(output),
            "--files-from",
            files_from,
        ])

    if rewrite_sanitizer_dylib:
        rc = subprocess.call([
            sys.executable,
            mozpath.join(topsrcdir, _REWRITE_SANITIZER_DYLIB),
            rewrite_sanitizer_dylib,
        ])
        if rc:
            return rc

    for source, output, pattern in archives:
        zip_action.main(["--error-if-empty", "-C", source, output, pattern])

    if libmegazord_artifacts:
        dist_bin, output = libmegazord_artifacts
        rc = package_libmegazord_artifacts.main(["--dist-bin", dist_bin, output])
        if rc:
            return rc

    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare a staged application tree for archiving."
    )
    parser.add_argument(
        "--staged-dir", required=True, help="Staged application tree to prepare"
    )
    parser.add_argument("--topsrcdir", required=True, help="Top source directory")
    parser.add_argument(
        "--symbol-archive",
        action="append",
        default=[],
        dest="symbol_archives",
        metavar="PATH",
        help="Archive an artifact build must have downloaded. Repeatable.",
    )
    parser.add_argument(
        "--find-dupes",
        dest="check_dupes",
        action="store_true",
        help="Check the staged tree for duplicated files",
    )
    parser.add_argument(
        "-D",
        dest="defines",
        action="append",
        default=[],
        metavar="VAR[=VAL]",
        help="Define a variable for the duplicate allowlist preprocessor. Repeatable.",
    )
    parser.add_argument(
        "--warn-dupes",
        action="store_true",
        help="Warn about unexpected duplicated files instead of failing",
    )
    parser.add_argument(
        "--allowed-dupes",
        action="append",
        default=[],
        metavar="PATH",
        help="File listing duplicates that are allowed. Repeatable.",
    )
    parser.add_argument(
        "--mozharness-zip", metavar="PATH", help="mozharness archive to write"
    )
    parser.add_argument(
        "--jsshell",
        nargs=3,
        metavar=("SOURCE", "OUTPUT", "FILES_FROM"),
        help="Archive the JavaScript shell files under SOURCE into OUTPUT. "
        "FILES_FROM lists the files, one path per line.",
    )
    parser.add_argument(
        "--rewrite-sanitizer-dylib",
        metavar="DIR",
        help="Rewrite sanitizer runtime dylib paths for the binaries in DIR",
    )
    parser.add_argument(
        "--archive",
        action="append",
        nargs=3,
        default=[],
        dest="archives",
        metavar=("SOURCE", "OUTPUT", "PATTERN"),
        help="Archive the files matching PATTERN under SOURCE into OUTPUT, "
        "which must not come out empty. Repeatable.",
    )
    parser.add_argument(
        "--libmegazord-artifacts",
        nargs=2,
        metavar=("DIST_BIN", "OUTPUT"),
        help="Archive the libmegazord artifacts in DIST_BIN into OUTPUT",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    return package_prepare(**vars(args))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
