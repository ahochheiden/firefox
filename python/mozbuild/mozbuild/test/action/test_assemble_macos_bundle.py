# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import unittest
from shutil import rmtree
from tempfile import mkdtemp

import mozunit
from mozfile import json

from mozbuild.action.assemble_macos_bundle import _stage, main


class TestAssembleMacOSBundle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = mkdtemp()

    def tearDown(self):
        rmtree(self.tmpdir)

    def _write(self, path, content=""):
        full = os.path.join(self.tmpdir, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full

    def test_stage(self):
        self._write("bin/XUL", "xul")
        self._write("bin/plugin-container.app/Contents/MacOS/plugin-container", "pc")
        self._write("bin/libfoo.dylib", "dylib")
        self._write("bin/omni.ja", "omni")
        macos_files = self._write("MacOS-files.txt", "*.app\nXUL\n")
        macos_copy = self._write("MacOS-files-copy.in", "# a comment\n*.dylib\n")
        contents = os.path.join(self.tmpdir, "Test.app", "Contents")
        os.makedirs(os.path.join(contents, "MacOS"))

        _stage(os.path.join(self.tmpdir, "bin"), contents, macos_files, macos_copy)

        macos = os.path.join(contents, "MacOS")
        resources = os.path.join(contents, "Resources")
        self.assertTrue(os.path.exists(os.path.join(macos, "XUL")))
        self.assertTrue(os.path.isdir(os.path.join(macos, "plugin-container.app")))
        self.assertTrue(os.path.exists(os.path.join(resources, "omni.ja")))
        self.assertTrue(os.path.exists(os.path.join(resources, "libfoo.dylib")))
        self.assertTrue(os.path.exists(os.path.join(macos, "libfoo.dylib")))
        self.assertFalse(os.path.exists(os.path.join(resources, "XUL")))
        self.assertFalse(os.path.exists(os.path.join(macos, "omni.ja")))

    def test_assemble_bundle(self):
        skeleton = os.path.join(self.tmpdir, "skeleton")
        self._write("skeleton/PkgInfo.in", "ignored")
        self._write("skeleton/Resources/ChannelPrefs.framework/data", "fw")
        info_plist = self._write("Info.plist", "plist")
        strings = self._write("InfoPlist.strings", "strings")
        binary = self._write("bin/firefox", "exe")
        icon = self._write("branding/firefox.icns", "icns")

        bundle = os.path.join(self.tmpdir, "Firefox.app")
        spec = {
            "bundle": bundle,
            "skeleton": skeleton,
            "info_plist": info_plist,
            "strings": strings,
            "lproj": "en.lproj",
            "binaries": [[binary, "firefox"]],
            "extra_files": [[icon, "Resources/firefox.icns"]],
            "move_to_frameworks": ["ChannelPrefs.framework"],
            "pkginfo": "APPLMOZB",
        }
        spec_path = self._write("spec.json", json.dumps(spec))
        self.assertEqual(main([spec_path]), 0)

        contents = os.path.join(bundle, "Contents")
        self.assertTrue(os.path.exists(os.path.join(contents, "Info.plist")))
        self.assertTrue(
            os.path.exists(
                os.path.join(contents, "Resources", "en.lproj", "InfoPlist.strings")
            )
        )
        self.assertTrue(os.path.exists(os.path.join(contents, "MacOS", "firefox")))
        self.assertTrue(
            os.path.exists(os.path.join(contents, "Resources", "firefox.icns"))
        )
        self.assertTrue(
            os.path.isdir(
                os.path.join(contents, "Frameworks", "ChannelPrefs.framework")
            )
        )
        self.assertFalse(
            os.path.exists(
                os.path.join(contents, "Resources", "ChannelPrefs.framework")
            )
        )
        self.assertFalse(os.path.exists(os.path.join(contents, "PkgInfo.in")))
        with open(os.path.join(contents, "PkgInfo")) as fh:
            self.assertEqual(fh.read(), "APPLMOZB")


if __name__ == "__main__":
    mozunit.main()
