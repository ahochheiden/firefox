# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import plistlib
import tarfile
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import mozunit

from mozbuild.action.make_appservices_xcframework import main


class TestMakeAppservicesXcframework(unittest.TestCase):
    def setUp(self):
        self._tmpdir = TemporaryDirectory()
        self.tmpdir = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def write_slice(self, name, binary, bindings):
        path = self.tmpdir / name
        path.mkdir()
        for megazord in ("FocusRustComponents", "MozillaRustComponents"):
            with zipfile.ZipFile(path / f"{megazord}.zip", "w") as z:
                framework = "MozillaRustComponents.framework"
                z.writestr(f"{framework}/MozillaRustComponents", f"{megazord}-{binary}")
                if bindings:
                    z.writestr(f"{framework}/Headers/loginsFFI.h", "header")
                    z.writestr(f"{framework}/Modules/module.modulemap", "modulemap")
                    z.writestr("Generated/logins.swift", f"{megazord}-swift")
                    z.writestr("Generated/Metrics/Metrics.swift", "metrics")
        return path

    def test_make_appservices_xcframework(self):
        output_dir = self.tmpdir / "out"
        self.assertEqual(
            main([
                "--device",
                str(self.write_slice("device", "device-binary", True)),
                "--simulator",
                str(self.write_slice("simulator", "simulator-binary", False)),
                "--output-dir",
                str(output_dir),
            ]),
            0,
        )

        with zipfile.ZipFile(output_dir / "MozillaRustComponents.xcframework.zip") as z:
            self.assertEqual(
                sorted(z.namelist()),
                [
                    "MozillaRustComponents.xcframework/Info.plist",
                    "MozillaRustComponents.xcframework/ios-arm64-simulator/MozillaRustComponents.framework/Headers/loginsFFI.h",
                    "MozillaRustComponents.xcframework/ios-arm64-simulator/MozillaRustComponents.framework/Modules/module.modulemap",
                    "MozillaRustComponents.xcframework/ios-arm64-simulator/MozillaRustComponents.framework/MozillaRustComponents",
                    "MozillaRustComponents.xcframework/ios-arm64/MozillaRustComponents.framework/Headers/loginsFFI.h",
                    "MozillaRustComponents.xcframework/ios-arm64/MozillaRustComponents.framework/Modules/module.modulemap",
                    "MozillaRustComponents.xcframework/ios-arm64/MozillaRustComponents.framework/MozillaRustComponents",
                ],
            )
            self.assertEqual(
                z.read(
                    "MozillaRustComponents.xcframework/ios-arm64-simulator/MozillaRustComponents.framework/MozillaRustComponents"
                ),
                b"MozillaRustComponents-simulator-binary",
            )
            self.assertEqual(
                plistlib.loads(z.read("MozillaRustComponents.xcframework/Info.plist")),
                {
                    "AvailableLibraries": [
                        {
                            "LibraryIdentifier": "ios-arm64",
                            "LibraryPath": "MozillaRustComponents.framework",
                            "SupportedArchitectures": ["arm64"],
                            "SupportedPlatform": "ios",
                        },
                        {
                            "LibraryIdentifier": "ios-arm64-simulator",
                            "LibraryPath": "MozillaRustComponents.framework",
                            "SupportedArchitectures": ["arm64"],
                            "SupportedPlatform": "ios",
                            "SupportedPlatformVariant": "simulator",
                        },
                    ],
                    "CFBundlePackageType": "XFWK",
                    "XCFrameworkFormatVersion": "1.0",
                },
            )

        with tarfile.open(output_dir / "swift-components.tar.xz") as tar:
            self.assertEqual(
                sorted(tar.getnames()),
                [
                    "swift-components/all/Generated/Metrics/Metrics.swift",
                    "swift-components/all/Generated/logins.swift",
                    "swift-components/focus/Generated/Metrics/Metrics.swift",
                    "swift-components/focus/Generated/logins.swift",
                ],
            )
            self.assertEqual(
                tar.extractfile("swift-components/focus/Generated/logins.swift").read(),
                b"FocusRustComponents-swift",
            )

        with zipfile.ZipFile(output_dir / "FocusRustComponents.xcframework.zip") as z:
            self.assertEqual(
                z.read(
                    "FocusRustComponents.xcframework/ios-arm64/MozillaRustComponents.framework/MozillaRustComponents"
                ),
                b"FocusRustComponents-device-binary",
            )


if __name__ == "__main__":
    mozunit.main()
