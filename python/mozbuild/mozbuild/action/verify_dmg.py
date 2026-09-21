# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import plistlib
import sys
from pathlib import Path

import buildconfig
import mozfile
from mozpack import dmg

from mozbuild.bootstrap import bootstrap_toolchain


def main(argv):
    parser = argparse.ArgumentParser(
        description="Check that a dmg opens and contains a launchable app bundle."
    )
    parser.add_argument("dmgfile", metavar="DMG", help="Disk image to check")
    parser.add_argument(
        "app_name", metavar="APP_NAME", help="Bundle expected at the image root"
    )
    args = parser.parse_args(argv)

    dmg_tool = buildconfig.substs.get("DMG_TOOL") or bootstrap_toolchain("dmg/dmg")
    hfs_tool = buildconfig.substs.get("HFS_TOOL") or bootstrap_toolchain("dmg/hfsplus")

    with mozfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        dmg.extract_dmg_contents(Path(args.dmgfile), dest, dmg_tool, hfs_tool)
        contents = dest / args.app_name / "Contents"
        plist = contents / "Info.plist"
        if not plist.is_file():
            print(f"{args.dmgfile}: {args.app_name}/Contents/Info.plist not found")
            return 1
        with plist.open("rb") as fh:
            executable = plistlib.load(fh)["CFBundleExecutable"]
        binary = contents / "MacOS" / executable
        if not binary.is_file():
            print(f"{args.dmgfile}: {binary.relative_to(dest)} not found")
            return 1
    print(f"{args.dmgfile}: {args.app_name} opens and contains {executable}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
