# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

"""Stage files and localized data for Windows NSIS installers."""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from mozbuild.action import preprocessor

_PREPROCESS_LOCALE = "toolkit/mozapps/installer/windows/nsis/preprocess-locale.py"


def nsis_stage(
    config_dir: Path,
    installs: list[str],
    defines_in: str,
    defines_out: str,
    preprocessor_args: list[str],
    topsrcdir: Path,
    locale_args: list[str],
    ab_cd: str,
    preprocess_locale: bool,
    single_files: list[list[str]],
    convert_utf8: list[list[str]],
) -> int:
    shutil.rmtree(config_dir, ignore_errors=True)
    config_dir.mkdir(parents=True)

    for src in installs:
        shutil.copy(src, config_dir / Path(src).name)

    if defines_in:
        preprocessor.main(
            ["-Fsubstitution"]
            + list(preprocessor_args)
            + [defines_in, "-o", defines_out]
        )

    if preprocess_locale or single_files or convert_utf8:
        ppl = str(topsrcdir / _PREPROCESS_LOCALE)

    if preprocess_locale:
        if result := _run(
            [sys.executable, ppl, "--preprocess-locale", str(topsrcdir)]
            + list(locale_args)
            + [ab_cd, str(config_dir)]
        ):
            return result

    for properties, nlf in single_files:
        if result := _run(
            [sys.executable, ppl, "--preprocess-single-file", str(topsrcdir)]
            + list(locale_args)
            + [str(config_dir), properties, nlf]
        ):
            return result

    for src, dest in convert_utf8:
        if result := _run([sys.executable, ppl, "--convert-utf8-utf16le", src, dest]):
            return result

    return 0


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Stage a CONFIG_DIR with the files makensis needs."
    )
    parser.add_argument("--config-dir", required=True, type=Path)
    parser.add_argument(
        "--install",
        action="append",
        default=[],
        dest="installs",
        help="File to copy into CONFIG_DIR (basename preserved). Repeatable.",
    )
    parser.add_argument("--defines-in", default="", help="defines.nsi.in input")
    parser.add_argument("--defines-out", default="", help="defines.nsi output")
    parser.add_argument("--topsrcdir", type=Path, help="Top source directory")
    parser.add_argument(
        "--locale-arg",
        action="append",
        default=[],
        dest="locale_args",
        help="Verbatim PPL_LOCALE_ARGS token for the preprocess-locale.py "
        "invocation (a locale dir, or a --l10n-dir=... entry). Repeatable.",
    )
    parser.add_argument("--ab-cd", default="", help="The ab_cd locale code")
    parser.add_argument(
        "--preprocess-locale",
        action="store_true",
        help="Run preprocess-locale.py --preprocess-locale",
    )
    parser.add_argument(
        "--single-file",
        action="append",
        nargs=2,
        default=[],
        dest="single_files",
        metavar=("PROPERTIES", "NLF"),
        help="Run preprocess-locale.py --preprocess-single-file. Repeatable.",
    )
    parser.add_argument(
        "--convert-utf8",
        action="append",
        nargs=2,
        default=[],
        metavar=("SRC", "DEST"),
        help="Run preprocess-locale.py --convert-utf8-utf16le. Repeatable.",
    )
    args, preprocessor_args = parser.parse_known_args(argv)

    return nsis_stage(
        config_dir=args.config_dir,
        installs=args.installs,
        defines_in=args.defines_in,
        defines_out=args.defines_out,
        preprocessor_args=preprocessor_args,
        topsrcdir=args.topsrcdir,
        locale_args=args.locale_args,
        ab_cd=args.ab_cd,
        preprocess_locale=args.preprocess_locale,
        single_files=args.single_files,
        convert_utf8=args.convert_utf8,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
