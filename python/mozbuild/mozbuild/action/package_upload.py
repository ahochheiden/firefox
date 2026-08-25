# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Upload the package artifacts and their checksums."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

import buildconfig
import checksums
import mozpack.path as mozpath
import upload

from mozbuild.packaging import PackagePaths, files_to_upload

_DIGESTS = ("sha512", "md5", "sha1")


def package_upload(
    *,
    upload_path: Optional[str] = None,
    base_path: Optional[str] = None,
    files: Sequence[str] = (),
    checksum_file: Optional[str] = None,
    ab_cd: str = "en-US",
    pkg_dir: Optional[str] = None,
    pkg_path: str = "",
    simple_package_name: Optional[str] = None,
    extra_files: Sequence[str] = (),
    digests: Sequence[str] = _DIGESTS,
) -> int:
    substs = buildconfig.substs
    topsrcdir = buildconfig.topsrcdir
    topobjdir = buildconfig.topobjdir
    dist = mozpath.join(topobjdir, "dist")
    paths = PackagePaths(
        substs=substs,
        dist_dir=dist,
        ab_cd=ab_cd,
        pkg_dir=pkg_dir,
        pkg_path=pkg_path,
        simple_name=simple_package_name,
    )

    upload_path = upload_path or os.environ.get("UPLOAD_PATH")
    if not upload_path:
        print("UPLOAD_PATH is not set", file=sys.stderr)
        return 1
    if sys.platform == "win32":
        upload_path = upload.FixupMsysPath(upload_path)

    if not files:
        files = files_to_upload(
            substs=substs,
            paths=paths,
            topsrcdir=topsrcdir,
            topobjdir=topobjdir,
            ab_cd=ab_cd,
            extra_files=extra_files,
        )

    upload.CopyFilesLocally(
        upload_path, files, base_path=base_path or dist, verbose=True
    )

    checksum_file = checksum_file or paths.checksums_file
    checksum_dir = mozpath.dirname(checksum_file)
    if checksum_dir:
        os.makedirs(checksum_dir, exist_ok=True)
    checksums.process_files([upload_path], checksum_file, [*digests])

    upload.CopyFilesLocally(
        upload_path, [checksum_file], base_path=base_path or dist, verbose=True
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Upload the package artifacts and their checksums."
    )
    parser.add_argument(
        "--upload-path",
        metavar="DIR",
        help="Directory to copy the artifacts into. Defaults to UPLOAD_PATH.",
    )
    parser.add_argument(
        "--base-path",
        metavar="DIR",
        help="Preserve artifact paths relative to this directory",
    )
    parser.add_argument(
        "--file",
        action="append",
        default=[],
        dest="files",
        metavar="PATH",
        help="Artifact to upload. Repeatable, and replaces the computed list "
        "rather than adding to it.",
    )
    parser.add_argument("--checksum-file", metavar="PATH", help="Checksums to write")
    parser.add_argument("--ab-cd", default="en-US", help="The locale being uploaded")
    parser.add_argument("--pkg-dir", help="Staged package directory name")
    parser.add_argument("--pkg-path", default="", help="Prefix inside the dist folder")
    parser.add_argument(
        "--simple-package-name",
        metavar="NAME",
        help="Package basename to use in place of the configured one",
    )
    parser.add_argument(
        "--extra-file",
        action="append",
        default=[],
        dest="extra_files",
        metavar="PATH",
        help="Extra artifact to upload, relative to the dist folder, added to "
        "the computed list. Repeatable.",
    )
    parser.add_argument(
        "--digest",
        action="append",
        dest="digests",
        metavar="ALGORITHM",
        help=f"Checksum algorithm. Repeatable. Defaults to {', '.join(_DIGESTS)}.",
    )
    return parser


def main(argv: list[str]) -> int:
    kwargs = vars(_parser().parse_args(argv))
    kwargs["digests"] = kwargs["digests"] or _DIGESTS
    return package_upload(**kwargs)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
