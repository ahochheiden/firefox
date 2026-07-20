# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import unittest
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp
from unittest import mock

import mozunit

from mozbuild.action import nsis_build


class TestNsisBuild(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(mkdtemp())

    def tearDown(self):
        rmtree(self.tmpdir)

    def test_makensis_and_output_copy(self):
        config_dir = self.tmpdir / "instgen"
        config_dir.mkdir()
        output = self.tmpdir / "dist/bin/uninstall/helper.exe"

        calls = []

        def fake_run(argv, cwd=None, check=False):
            calls.append((argv, cwd))
            (Path(cwd) / "helper.exe").write_text("exe", encoding="utf-8")
            return mock.Mock(returncode=0)

        with mock.patch.object(nsis_build.subprocess, "run", side_effect=fake_run):
            rc = nsis_build.main([
                "--config-dir",
                str(config_dir),
                "--nsi",
                "uninstaller.nsi",
                "--makensis",
                "makensis",
                "--makensis-flag=-nocd",
                "--produced",
                "helper.exe",
                "--output",
                str(output),
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        argv, cwd = calls[0]
        self.assertEqual(argv, ["makensis", "-nocd", "uninstaller.nsi"])
        self.assertEqual(Path(cwd), config_dir)
        self.assertEqual(output.read_text(encoding="utf-8"), "exe")

    def test_no_output_leaves_exe_in_config_dir(self):
        # Installer and stub outputs remain in CONFIG_DIR for repackaging.
        config_dir = self.tmpdir / "instgen"
        config_dir.mkdir()

        calls = []

        def fake_run(argv, cwd=None, check=False):
            calls.append((argv, cwd))
            (Path(cwd) / "setup.exe").write_text("exe", encoding="utf-8")
            return mock.Mock(returncode=0)

        with mock.patch.object(nsis_build.subprocess, "run", side_effect=fake_run):
            rc = nsis_build.main([
                "--config-dir",
                str(config_dir),
                "--nsi",
                "installer.nsi",
                "--makensis",
                "makensis",
            ])

        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], ["makensis", "installer.nsi"])
        self.assertTrue((config_dir / "setup.exe").exists())

    def test_makensis_failure_skips_copy(self):
        config_dir = self.tmpdir / "instgen"
        config_dir.mkdir()
        # A failed build must not copy a stale output.
        (config_dir / "helper.exe").write_text("stale", encoding="utf-8")
        output = self.tmpdir / "dist/bin/uninstall/helper.exe"

        with mock.patch.object(
            nsis_build.subprocess,
            "run",
            return_value=mock.Mock(returncode=3),
        ):
            rc = nsis_build.main([
                "--config-dir",
                str(config_dir),
                "--nsi",
                "uninstaller.nsi",
                "--makensis",
                "makensis",
                "--produced",
                "helper.exe",
                "--output",
                str(output),
            ])

        self.assertEqual(rc, 3)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    mozunit.main()
