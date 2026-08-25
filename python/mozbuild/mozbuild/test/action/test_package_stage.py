# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import tempfile
import unittest
from unittest import mock

import mozunit
from mozpack.errors import AccumulatedErrors, ErrorMessage, errors

from mozbuild.action import package_stage

_SUBSTS = {"OS_ARCH": "Linux"}
_DEFINES = {"ALLDEFINES": {}}


class TestPackageStageMain(unittest.TestCase):
    def _run(self, argv):
        with mock.patch.object(package_stage, "package_stage", return_value=0) as m:
            rc = package_stage.main(argv)
        self.assertEqual(rc, 0)
        return m.call_args.kwargs

    def test_positional_arguments(self):
        kwargs = self._run(["package-manifest.in", "dist", "dist/firefox"])

        self.assertEqual(kwargs["manifest"], "package-manifest.in")
        self.assertEqual(kwargs["source"], "dist")
        self.assertEqual(kwargs["destination"], "dist/firefox")

    def test_no_manifest(self):
        kwargs = self._run(["dist", "dist/firefox"])

        self.assertIsNone(kwargs["manifest"])
        self.assertEqual(kwargs["source"], "dist")
        self.assertEqual(kwargs["destination"], "dist/firefox")

    def test_options(self):
        kwargs = self._run([
            "-DAB_CD=de",
            "-DMOZ_OMNIJAR=1",
            "--format",
            "flat",
            "--removals",
            "removed-files.in",
            "--ignore-errors",
            "--ignore-broken-symlinks",
            "--minify",
            "--minify-js",
            "--minify-pdfjs",
            "--jarlog",
            "jarlog/en-US.log",
            "--compress",
            "none",
            "--no-pkg-files",
            "mangle*",
            "--no-pkg-files",
            "*.dSYM",
            "package-manifest.in",
            "dist",
            "dist/firefox",
            "--non-resource",
            "defaults/*",
            "greprefs.js",
        ])

        self.assertEqual(kwargs["defines"], ["AB_CD=de", "MOZ_OMNIJAR=1"])
        self.assertEqual(kwargs["package_format"], "flat")
        self.assertEqual(kwargs["removals"], "removed-files.in")
        self.assertTrue(kwargs["ignore_errors"])
        self.assertTrue(kwargs["ignore_broken_symlinks"])
        self.assertTrue(kwargs["minify"])
        self.assertTrue(kwargs["minify_js"])
        self.assertTrue(kwargs["minify_pdfjs"])
        self.assertEqual(kwargs["jarlog"], "jarlog/en-US.log")
        self.assertEqual(kwargs["compress"], "none")
        self.assertEqual(kwargs["no_pkg_files"], ["mangle*", "*.dSYM"])
        self.assertEqual(kwargs["non_resources"], ["defaults/*", "greprefs.js"])

    def test_defaults(self):
        kwargs = self._run(["dist", "dist/firefox"])

        self.assertEqual(kwargs["defines"], [])
        self.assertEqual(kwargs["package_format"], "omni")
        self.assertIsNone(kwargs["removals"])
        self.assertIsNone(kwargs["jarlog"])
        self.assertEqual(kwargs["compress"], "deflate")
        self.assertIsNone(kwargs["no_pkg_files"])
        self.assertEqual(kwargs["non_resources"], [])
        self.assertFalse(kwargs["ignore_errors"])

    def test_return_code(self):
        with mock.patch.object(package_stage, "package_stage", return_value=3):
            rc = package_stage.main(["dist", "staged"])

        self.assertEqual(rc, 3)


class TestPackageStage(unittest.TestCase):
    def _tree(self, root):
        source = os.path.join(root, "dist")
        os.makedirs(os.path.join(source, "bin"))
        for name in ("foo.txt", "bar.txt", "nsinstall"):
            with open(os.path.join(source, "bin", name), "w") as f:
                f.write(name)
        removals = os.path.join(root, "removed-files.in")
        with open(removals, "w") as f:
            f.write("foo.txt\n")
        return source, removals

    def _stage(self, source, destination, package_format="flat", **kwargs):
        with mock.patch.object(package_stage.buildconfig, "defines", _DEFINES):
            with mock.patch.object(package_stage.buildconfig, "substs", _SUBSTS):
                return package_stage.package_stage(
                    source=source,
                    destination=destination,
                    package_format=package_format,
                    **kwargs,
                )

    def _staged(self, destination):
        return sorted(
            os.path.relpath(os.path.join(d, n), destination)
            for d, _, names in os.walk(destination)
            for n in names
        )

    def test_no_manifest_stages_bin(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = self._tree(tmp)
            staged = os.path.join(tmp, "staged")

            self.assertEqual(self._stage(source, staged), 0)

            self.assertEqual(
                self._staged(staged), ["bar.txt", "foo.txt", "nsinstall", "precomplete"]
            )

    def test_ignore_errors_per_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, removals = self._tree(tmp)

            lenient = os.path.join(tmp, "lenient")
            self.assertEqual(
                self._stage(source, lenient, removals=removals, ignore_errors=True), 0
            )
            self.assertIn("removed-files", self._staged(lenient))

            with self.assertRaises(AccumulatedErrors):
                self._stage(source, os.path.join(tmp, "strict"), removals=removals)

            self.assertEqual(errors._level, errors.ERROR)

    def test_error_policy_restored_on_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = self._tree(tmp)

            with self.assertRaises(ErrorMessage):
                self._stage(
                    source,
                    os.path.join(tmp, "unknown-format"),
                    package_format="bogus",
                    ignore_errors=True,
                )

            self.assertEqual(errors._level, errors.ERROR)

    def test_no_pkg_files_per_invocation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, _ = self._tree(tmp)

            filtered = os.path.join(tmp, "filtered")
            self.assertEqual(
                self._stage(source, filtered, no_pkg_files=["nsinstall*"]), 0
            )
            self.assertNotIn("nsinstall", self._staged(filtered))

            unfiltered = os.path.join(tmp, "unfiltered")
            self.assertEqual(self._stage(source, unfiltered), 0)
            self.assertIn("nsinstall", self._staged(unfiltered))


if __name__ == "__main__":
    mozunit.main()
