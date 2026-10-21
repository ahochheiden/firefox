# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import plistlib
import sys
import tarfile
from io import BytesIO
from pathlib import Path

from mozpack.mozjar import JarReader, JarWriter

FRAMEWORK = "MozillaRustComponents.framework"
BINARY = f"{FRAMEWORK}/MozillaRustComponents"
SWIFT_COMPONENTS = {
    "FocusRustComponents": "focus",
    "MozillaRustComponents": "all",
}


def make_xcframework(name, slices, output_dir, swift_components):
    libraries = []
    with JarWriter(file=str(output_dir / f"{name}.xcframework.zip")) as xcframework:
        for member in JarReader(str(Path(slices[0][1]) / f"{name}.zip")):
            if member.filename.startswith("Generated/"):
                data = member.read()
                info = tarfile.TarInfo(
                    f"swift-components/{SWIFT_COMPONENTS[name]}/{member.filename}"
                )
                info.size = len(data)
                swift_components.addfile(info, BytesIO(data))
            elif member.filename != BINARY:
                for identifier, _, _ in slices:
                    xcframework.add(
                        f"{name}.xcframework/{identifier}/{member.filename}", member
                    )
        for identifier, archive_dir, extra in slices:
            archive = JarReader(str(Path(archive_dir) / f"{name}.zip"))
            xcframework.add(
                f"{name}.xcframework/{identifier}/{BINARY}", archive[BINARY]
            )
            libraries.append({
                "LibraryIdentifier": identifier,
                "LibraryPath": FRAMEWORK,
                "SupportedArchitectures": ["arm64"],
                "SupportedPlatform": "ios",
                **extra,
            })

        info_plist = BytesIO()
        plistlib.dump(
            {
                "AvailableLibraries": libraries,
                "CFBundlePackageType": "XFWK",
                "XCFrameworkFormatVersion": "1.0",
            },
            info_plist,
        )
        xcframework.add(f"{name}.xcframework/Info.plist", info_plist.getvalue())


def main(argv):
    parser = argparse.ArgumentParser(
        description="Combine the per target application-services iOS archives "
        "into XCFrameworks."
    )
    parser.add_argument(
        "--device",
        required=True,
        help="Directory holding the archives built for aarch64-apple-ios",
    )
    parser.add_argument(
        "--simulator",
        required=True,
        help="Directory holding the archives built for aarch64-apple-ios-sim",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Directory to write the XCFramework and Swift sources archives to",
    )
    args = parser.parse_args(argv)

    slices = [
        ("ios-arm64", args.device, {}),
        (
            "ios-arm64-simulator",
            args.simulator,
            {"SupportedPlatformVariant": "simulator"},
        ),
    ]
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output_dir / "swift-components.tar.xz", "w:xz") as tar:
        for name in SWIFT_COMPONENTS:
            make_xcframework(name, slices, output_dir, tar)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
