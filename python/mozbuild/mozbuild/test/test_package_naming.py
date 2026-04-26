# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import unittest

import mozunit

from mozbuild.package_naming import (
    langpack_eid,
    pkg_basename,
    pkg_inst_basename,
    pkg_langpack_basename,
    pkg_langpack_path,
    pkg_path,
    pkg_suffix,
)


def _substs(**overrides):
    base = {
        "MOZ_PKG_APPNAME": "firefox",
        "MOZ_PKG_VERSION": "152.0a1",
        "MOZ_PKG_PLATFORM": "win64",
        "MOZ_PKG_FORMAT": "ZIP",
        "MOZ_LANGPACK_EID_HOST": "firefox.mozilla.org",
    }
    base.update(overrides)
    return base


class TestPackageNaming(unittest.TestCase):
    def test_pkg_basename_default(self):
        self.assertEqual(
            pkg_basename(_substs(), "en-US"),
            "firefox-152.0a1.en-US.win64",
        )
        self.assertEqual(
            pkg_basename(_substs(), "de"),
            "firefox-152.0a1.de.win64",
        )

    def test_pkg_basename_simple_override(self):
        substs = _substs(MOZ_SIMPLE_PACKAGE_NAME="my-package")
        self.assertEqual(pkg_basename(substs, "en-US"), "my-package")
        self.assertEqual(pkg_basename(substs, "de"), "my-package")

    def test_pkg_basename_pkg_appname_override(self):
        substs = _substs(MOZ_PKG_APPNAME="custom")
        self.assertEqual(
            pkg_basename(substs, "en-US"),
            "custom-152.0a1.en-US.win64",
        )

    def test_pkg_basename_pkg_version_override(self):
        substs = _substs(MOZ_PKG_VERSION="200.0")
        self.assertEqual(
            pkg_basename(substs, "en-US"),
            "firefox-200.0.en-US.win64",
        )

    def test_pkg_path_default(self):
        self.assertEqual(pkg_path(_substs()), "")

    def test_pkg_suffix(self):
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="TAR")), ".tar")
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="TGZ")), ".tar.gz")
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="XZ")), ".tar.xz")
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="BZ2")), ".tar.bz2")
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="ZIP")), ".zip")
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="DMG")), ".dmg")
        self.assertEqual(pkg_suffix(_substs(MOZ_PKG_FORMAT="APK")), "")

    def test_pkg_langpack_basename_default(self):
        self.assertEqual(
            pkg_langpack_basename(_substs(), "de"),
            "firefox-152.0a1.de.langpack",
        )

    def test_pkg_langpack_basename_simple_override(self):
        substs = _substs(MOZ_SIMPLE_PACKAGE_NAME="my-package")
        self.assertEqual(
            pkg_langpack_basename(substs, "de"),
            "my-package.langpack",
        )

    def test_pkg_langpack_path_default(self):
        self.assertEqual(pkg_langpack_path(_substs()), "win64/xpi/")

    def test_pkg_langpack_path_simple_override(self):
        substs = _substs(MOZ_SIMPLE_PACKAGE_NAME="my-package")
        self.assertEqual(pkg_langpack_path(substs), "")

    def test_pkg_inst_basename(self):
        self.assertEqual(
            pkg_inst_basename(_substs(), "en-US"),
            "firefox-152.0a1.en-US.win64.installer",
        )

    def test_langpack_eid_default(self):
        self.assertEqual(
            langpack_eid(_substs(), "de"),
            "langpack-de@firefox.mozilla.org",
        )

    def test_langpack_eid_alternate_host(self):
        substs = _substs(MOZ_LANGPACK_EID_HOST="devedition.mozilla.org")
        self.assertEqual(
            langpack_eid(substs, "de"),
            "langpack-de@devedition.mozilla.org",
        )


if __name__ == "__main__":
    mozunit.main()
